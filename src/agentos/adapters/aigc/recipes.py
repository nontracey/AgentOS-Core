"""Versioned, parameterized ComfyUI graphs; users never submit arbitrary code."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agentos.domains.aigc.sampling import REFERENCE_CLEANUP_RECIPE, default_image_steps, validate_edit_region
from agentos.kernel.errors import AgentOSError, ErrorCode

RECIPE_DIR = Path(__file__).with_name('recipes')
LIGHTNING_RECIPE = 'qwen-edit-2511-lightning'
RECIPE_LORAS = {LIGHTNING_RECIPE: {
    'filename': 'Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors',
    'sha256': '22226e8d05d354bb356627d428809f5afd7819399b077238a2b70a82883a904f',
}}
REFERENCE_CLEANUP_MODEL = {
    'filename': 'big-lama.pt',
    'sha256': '344c77bbcb158f17dd143070d1e789f38a66c04202311ae3a258ef66667a9ea9',
}
REFERENCE_CLEANUP_VERSION = (
    'Acly/5b7681746fe19e563113d27366ba85236bc1ba5b/'
    'nodes-eb11b7ddb5a8be81bfbaa9879659eb52ecf8776089bf20d9755eac41691b2077/lama256-context-v1')
RECIPES = {
    'pet-motion': {'file': 'wan22-pet.json', 'label': '桌宠动作', 'transparent': True},
    'story-shot-quality': {'file': 'wan22-shot-quality.json', 'label': '短剧质量候选（20 步）', 'transparent': False},
    'story-shot': {'file': 'wan22-shot.json', 'label': '短剧镜头', 'transparent': False},
    'story-shot-guided': {'file': 'wan22-guided.json', 'label': '短剧首尾帧候选（4 步）', 'transparent': False},
    'story-shot-guided-quality': {'file': 'wan22-guided-quality.json',
                                  'label': '短剧首尾帧质量候选（20 步）', 'transparent': False},
}


IMAGE_RECIPES: dict[str, dict[str, Any]] = {
    REFERENCE_CLEANUP_RECIPE: {'label': '参考源清理候选 · 非扩散 CNN，不绘制新角色',
        'references': True, 'kind': 'reference-preparation', 'version': REFERENCE_CLEANUP_VERSION},
    'klein-image': {'file': 'klein-image.json', 'label': '固定本地模型 · 多参考编辑', 'references': 'optional'},
    'qwen-style-v2': {'file': 'qwen-style-v2.json', 'label': 'TeleStyle V2 · 原尺寸参考与动态采样', 'references': True},
    'qwen-style': {'file': 'qwen-style.json', 'label': 'TeleStyle V2 · 内容与画风分离', 'references': True},
    'qwen-image': {'file': 'qwen-image.json', 'label': '本地角色与场景生成', 'references': False},
    'qwen-edit': {'file': 'qwen-edit.json', 'label': '本地多参考图编辑（2509）', 'references': True},
    'qwen-edit-2511': {'file': 'qwen-edit-2511.json', 'label': '本地多参考图编辑（2511）', 'references': True},
    LIGHTNING_RECIPE: {'file': 'qwen-edit-2511.json', 'label': '2511 官方四步编辑候选', 'references': True},
}

def image_graph(args: dict[str, Any], images: list[str], output_prefix: str,
                *, lora_name: str | None = None, cleanup: dict[str, Any] | None = None) -> dict[str, Any]:
    recipe = str(args.get('recipe', 'klein-image'))
    if recipe not in IMAGE_RECIPES:
        raise AgentOSError(ErrorCode.INVALID, 'unknown image recipe')
    if recipe == REFERENCE_CLEANUP_RECIPE:
        if lora_name is not None:
            raise AgentOSError(ErrorCode.INVALID, 'reference cleanup does not use a diffusion LoRA')
        return reference_cleanup_graph(args, images, output_prefix, cleanup)
    if not args.get('image_lora_id') and 'image_lora_strength' in args:
        raise AgentOSError(ErrorCode.INVALID, 'image LoRA strength requires a registered ID')
    lightning = recipe == LIGHTNING_RECIPE
    if lightning and lora_name != RECIPE_LORAS[recipe]['filename']:
        raise AgentOSError(ErrorCode.INVALID, 'Lightning recipe requires its registered official preset')
    if not lightning and bool(args.get('image_lora_id')) != bool(lora_name):
        raise AgentOSError(ErrorCode.INVALID, 'registered image LoRA must be resolved before graph construction')
    if lora_name and (recipe not in {'klein-image', LIGHTNING_RECIPE} or Path(lora_name).name != lora_name or
                      any(char in lora_name for char in '/\\:') or not lora_name.endswith('.safetensors')):
        raise AgentOSError(ErrorCode.INVALID, 'image LoRA requires a registered Klein model basename')
    prompt = str(args.get('prompt', '')).strip()
    width, height = args.get('width', 768), args.get('height', 1024)
    seed, steps = args.get('seed', 1), args.get('steps', default_image_steps(recipe))
    if (not prompt or len(prompt) > 4000
            or any(type(x) is not int for x in (width, height, seed, steps))
            or not 256 <= width <= 1024 or not 256 <= height <= 1024
            or width % 16 or height % 16 or not 0 <= seed <= 2**63 - 1 or not 4 <= steps <= 60
            or len(images) > 3 or (recipe != 'klein-image' and
                bool(images) != IMAGE_RECIPES[recipe]['references'])):
        raise AgentOSError(ErrorCode.INVALID, 'invalid image recipe parameters or references')
    if recipe == 'klein-image':
        if steps != 4:
            raise AgentOSError(ErrorCode.INVALID, 'fixed distilled Klein recipe requires four native steps')
        graph: dict[str, Any] = json.loads((RECIPE_DIR / 'klein-image.json').read_text())
        graph['7']['inputs']['text'] = prompt
        graph['8']['inputs']['text'] = str(args.get('negative', ''))
        for node in ('11', '12'):
            graph[node]['inputs'].update(width=width, height=height)
        graph['14']['inputs']['noise_seed'] = seed
        graph['16']['inputs']['filename_prefix'] = output_prefix
        positive, negative = ['7', 0], ['8', 0]
        for index, name in enumerate(images):
            load, encode, pos, neg = map(str, range(40 + index * 4, 44 + index * 4))
            graph[load] = {'class_type': 'LoadImage', 'inputs': {'image': name}}
            graph[encode] = {'class_type': 'VAEEncode', 'inputs': {'pixels': [load, 0], 'vae': ['5', 0]}}
            graph[pos] = {'class_type': 'ReferenceLatent',
                          'inputs': {'conditioning': positive, 'latent': [encode, 0]}}
            graph[neg] = {'class_type': 'ReferenceLatent',
                          'inputs': {'conditioning': negative, 'latent': [encode, 0]}}
            positive, negative = [pos, 0], [neg, 0]
        graph['13']['inputs'].update(positive=positive, negative=negative)
        if lora_name:
            strength = args.get('image_lora_strength', 1)
            if type(strength) not in (int, float) or not 0 <= strength <= 1.5:
                raise AgentOSError(ErrorCode.INVALID, 'invalid image LoRA strength')
            graph['18'] = {'class_type': 'LoraLoaderModelOnly',
                'inputs': {'model': ['3', 0], 'lora_name': lora_name, 'strength_model': strength}}
            graph['13']['inputs']['model'] = ['18', 0]
        return regional_image_graph(graph, args, images, width, height)
    if lightning and (steps != 4 or args.get('image_lora_id')):
        raise AgentOSError(ErrorCode.INVALID, 'Lightning requires four steps and its fixed recipe LoRA')
    if recipe in {'qwen-style', 'qwen-style-v2'} and (len(images) != 2 or steps != 4):
        raise AgentOSError(ErrorCode.INVALID,
            'style transfer requires content first, style second and four distilled steps')
    graph = json.loads((RECIPE_DIR / str(IMAGE_RECIPES[recipe]['file'])).read_text())
    field = 'prompt' if images else 'text'
    graph['7']['inputs'][field] = prompt
    if '8' in graph:
        graph['8']['inputs'][field] = str(args.get('negative', 'text, watermark, malformed hands'))
    if recipe == 'qwen-style-v2':
        graph['9']['inputs'].update(width=width, height=height)
    graph['10']['inputs'].update(width=width, height=height)
    graph['11']['inputs'].update(seed=seed, steps=steps)
    if lightning:
        graph['18'] = {'class_type': 'LoraLoaderModelOnly', 'inputs': {
            'model': ['4', 0], 'lora_name': lora_name, 'strength_model': 1.0}}
        graph['9']['inputs']['model'] = ['18', 0]
        graph['11']['inputs']['cfg'] = 1.0
    graph['16']['inputs']['filename_prefix'] = output_prefix
    if images:
        for index, name in enumerate(images):
            node = str(1 if index == 0 else 20 + index)
            graph[node] = {'class_type': 'LoadImage', 'inputs': {'image': name}}
            for encoder in ('7', '8'):
                if encoder not in graph:
                    continue
                graph[encoder]['inputs']['image' + str(index + 1)] = [node, 0]
    return regional_image_graph(graph, args, images, width, height)


def reference_cleanup_graph(args: dict[str, Any], images: list[str], output_prefix: str,
                            geometry: dict[str, Any] | None) -> dict[str, Any]:
    allowed = {'recipe', 'prompt', 'reference_asset_ids', 'reference_asset_refs', 'edit_region'}
    if (set(args).difference(allowed) or len(images) != 2 or not str(args.get('prompt', '')).strip()
            or len(str(args.get('prompt', ''))) > 4000):
        raise AgentOSError(ErrorCode.INVALID, 'cleanup accepts only source, mask, context region and review text')
    try:
        validate_edit_region(args.get('edit_region'))
    except ValueError as exc:
        raise AgentOSError(ErrorCode.INVALID, 'cleanup requires a normalized context region') from exc
    if geometry is None or set(geometry) != {'crop', 'empty'}:
        raise AgentOSError(ErrorCode.INVALID, 'cleanup requires validated source and mask geometry')
    crop = geometry['crop']
    if (not isinstance(crop, (tuple, list)) or len(crop) != 4 or any(type(n) is not int for n in crop)
            or min(crop[:2]) < 0 or min(crop[2:]) < 1 or type(geometry['empty']) is not bool):
        raise AgentOSError(ErrorCode.INVALID, 'invalid cleanup context geometry')
    graph: dict[str, Any] = {
        '1': {'class_type': 'LoadImage', 'inputs': {'image': images[0]}},
        '2': {'class_type': 'LoadImage', 'inputs': {'image': images[1]}},
        '16': {'class_type': 'SaveImage', 'inputs': {'images': ['1', 0], 'filename_prefix': output_prefix}},
    }
    if geometry['empty']:
        return graph
    x, y, width, height = crop
    graph.update({
        '3': {'class_type': 'ImageToMask', 'inputs': {'image': ['2', 0], 'channel': 'red'}},
        '4': {'class_type': 'INPAINT_LoadInpaintModel', 'inputs': {
            'model_name': REFERENCE_CLEANUP_MODEL['filename']}},
        '5': {'class_type': 'ImageCrop', 'inputs': {
            'image': ['1', 0], 'x': x, 'y': y, 'width': width, 'height': height}},
        '6': {'class_type': 'CropMask', 'inputs': {
            'mask': ['3', 0], 'x': x, 'y': y, 'width': width, 'height': height}},
        '7': {'class_type': 'INPAINT_InpaintWithModel', 'inputs': {
            'inpaint_model': ['4', 0], 'image': ['5', 0], 'mask': ['6', 0], 'seed': 0, 'lama_size': 256}},
        '8': {'class_type': 'ImageCompositeMasked', 'inputs': {
            'destination': ['1', 0], 'source': ['7', 0], 'x': x, 'y': y,
            'resize_source': False, 'mask': ['6', 0]}},
    })
    graph['16']['inputs']['images'] = ['8', 0]
    return graph


def regional_image_graph(graph: dict[str, Any], args: dict[str, Any], images: list[str],
                         width: int, height: int) -> dict[str, Any]:
    if 'edit_region' not in args:
        return graph
    try:
        region = validate_edit_region(args['edit_region'])
    except ValueError as exc:
        raise AgentOSError(ErrorCode.INVALID, 'invalid normalized image edit region') from exc
    if len(images) != 1:
        raise AgentOSError(ErrorCode.INVALID, 'regional image editing requires exactly one source reference')
    left, top, right, bottom = [round(point * extent) for point, extent in
                                zip(region, [width, height, width, height], strict=True)]
    w, h = right - left, bottom - top
    if w < 1 or h < 1:
        raise AgentOSError(ErrorCode.INVALID, 'image edit region contains no output pixels')
    original = next(id for id, node in graph.items() if node['class_type'] == 'LoadImage')
    decoded = graph['16']['inputs']['images']
    vae = graph[decoded[0]]['inputs']['vae']
    sampler = next(id for id, node in graph.items() if node['class_type'] in {'KSampler', 'SamplerCustomAdvanced'})
    graph.update({
        '70': {'class_type': 'ImageScale', 'inputs': {'image': [original, 0], 'upscale_method': 'lanczos',
                                                     'width': width, 'height': height, 'crop': 'disabled'}},
        '71': {'class_type': 'SolidMask', 'inputs': {'value': 0.0, 'width': width, 'height': height}},
        '72': {'class_type': 'SolidMask', 'inputs': {'value': 1.0, 'width': w, 'height': h}},
        '73': {'class_type': 'FeatherMask', 'inputs': {'mask': ['72', 0],
            'left': min(12, w // 2) if left else 0, 'top': min(12, h // 2) if top else 0,
            'right': min(12, w // 2) if right < width else 0,
            'bottom': min(12, h // 2) if bottom < height else 0}},
        '74': {'class_type': 'MaskComposite', 'inputs': {'destination': ['71', 0], 'source': ['73', 0],
                                                       'x': left, 'y': top, 'operation': 'add'}},
        '75': {'class_type': 'ImageCompositeMasked', 'inputs': {'destination': ['70', 0], 'source': decoded,
            'x': 0, 'y': 0, 'resize_source': False, 'mask': ['74', 0]}},
        '76': {'class_type': 'VAEEncode', 'inputs': {'pixels': ['70', 0], 'vae': vae}},
        '77': {'class_type': 'SetLatentNoiseMask', 'inputs': {'samples': ['76', 0], 'mask': ['74', 0]}},
    })
    graph[sampler]['inputs']['latent_image'] = ['77', 0]
    graph['16']['inputs']['images'] = ['75', 0]
    return graph

def recipe_graph(args: dict[str, Any], image_name: str, output_prefix: str,
                 *, end_image_name: str | None = None) -> dict[str, Any]:
    recipe = str(args.get('recipe', 'story-shot'))
    if recipe not in RECIPES:
        raise AgentOSError(ErrorCode.INVALID, 'unknown production recipe')
    guided = recipe in {'story-shot-guided', 'story-shot-guided-quality'}
    if guided and not end_image_name:
        raise AgentOSError(ErrorCode.INVALID, 'guided video requires an independent end image')
    if not guided and (end_image_name or args.get('end_asset_id') or args.get('end_asset_ref')):
        raise AgentOSError(ErrorCode.INVALID, 'this video recipe does not consume an end image')
    prompt = str(args.get('prompt', '')).strip()
    if not prompt or len(prompt) > 4000:
        raise AgentOSError(ErrorCode.INVALID, 'prompt must contain 1–4000 characters')
    width, height = args.get('width', 512), args.get('height', 800)
    frames, fps, seed = args.get('frames', 65), args.get('fps', 16), args.get('seed', 1)
    if (any(type(x) is not int for x in (width, height, frames, fps, seed))
            or not 256 <= width <= 1024 or not 256 <= height <= 1024
            or width % 16 or height % 16 or not 5 <= frames <= 81 or (frames - 1) % 4
            or not 1 <= fps <= 30 or not 0 <= seed <= 2**63 - 1):
        raise AgentOSError(ErrorCode.INVALID, 'invalid dimensions, frame count, fps or seed')
    graph: dict[str, Any] = json.loads((RECIPE_DIR / str(RECIPES[recipe]['file'])).read_text())
    graph['1']['inputs']['image'] = image_name
    graph['2']['inputs'].update(width=width, height=height)
    if guided:
        graph['20']['inputs']['image'] = end_image_name
        graph['21']['inputs'].update(width=width, height=height)
    graph['7']['inputs']['text'] = prompt
    graph['8']['inputs']['text'] = str(args.get('negative', 'deformed limbs, extra fingers, cropped body'))
    graph['10']['inputs'].update(width=width, height=height, length=frames)
    for node in ('11', '14'):
        graph[node]['inputs']['noise_seed'] = seed
    graph['16']['inputs']['fps'] = fps
    for node in ('3', '16', '24'):
        if node in graph:
            graph[node]['inputs']['filename_prefix'] = output_prefix + '/' + node
    return graph


def fingerprint(args: dict[str, Any], input_hashes: list[str]) -> str:
    recipe = str(args.get('recipe', 'story-shot'))
    recipes = {**RECIPES, **IMAGE_RECIPES}
    if recipe not in recipes:
        raise AgentOSError(ErrorCode.INVALID, 'unknown production recipe')
    # Include recipe bytes: editing a graph cannot silently reuse a previous result.
    graph = (json.dumps(recipes[recipe], sort_keys=True).encode() if recipe == REFERENCE_CLEANUP_RECIPE else
             (RECIPE_DIR / str(recipes[recipe]['file'])).read_bytes())
    encoded = json.dumps(args, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(graph + b'\0' + encoded + b'\0' + '\n'.join(input_hashes).encode()).hexdigest()
