"""Production tools for an existing Windows node and managed ComfyUI endpoint.

ComfyUI owns graph execution; local records only reconcile submissions and outputs.
AgentOS/Temporal owns production dependencies, approval and retry decisions.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageChops

from agentos.adapters.aigc.media import (
    asset_file,
    compose,
    ffmpeg_run,
    mix_dialogue,
    normalize_clip,
    pack_alpha,
    prepare_image,
    prepare_reference,
    probe_duration,
    register_output,
    save_json,
)
from agentos.adapters.aigc.recipes import (
    IMAGE_RECIPES,
    LIGHTNING_RECIPE,
    RECIPE_LORAS,
    RECIPES,
    REFERENCE_CLEANUP_MODEL,
    REFERENCE_CLEANUP_VERSION,
    fingerprint,
    image_graph,
    recipe_graph,
)
from agentos.domains.aigc.sampling import REFERENCE_CLEANUP_RECIPE, validate_edit_region
from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.types import AuthContext, CallContext, ToolSpec, utc_now

MAX_BYTES = 512 * 1024**2


class AigcToolAdapter:
    def __init__(self, *, base_url: str, sandbox_dir: Path, ffmpeg: str,
                 speech_command: list[str] | None = None, lipsync_command: list[str] | None = None,
                 image_loras: dict[str, dict[str, str]] | None = None,
                 recipe_loras: dict[str, dict[str, str]] | None = None,
                 recipe_models: dict[str, dict[str, str]] | None = None,
                 timeout_s: float = 5460) -> None:
        self.base = base_url.rstrip('/')
        self.assets = sandbox_dir / 'assets'
        self.jobs = sandbox_dir / 'aigc-jobs'
        self.ffmpeg, self.speech_command, self.timeout = ffmpeg, speech_command, timeout_s
        self.lipsync_command = lipsync_command
        self.image_loras = dict(image_loras or {})
        self.recipe_loras = dict(recipe_loras or {})
        self.recipe_models = dict(recipe_models or {})
        if set(self.recipe_models).difference({REFERENCE_CLEANUP_RECIPE}):
            raise AgentOSError(ErrorCode.INVALID, 'unknown registered recipe model')
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', key) for key in self.image_loras):
            raise AgentOSError(ErrorCode.INVALID, 'invalid registered image LoRA ID')
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', key) for key in self.recipe_loras):
            raise AgentOSError(ErrorCode.INVALID, 'invalid registered recipe LoRA ID')
        self.jobs.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def has_tool(self, name: str) -> bool:
        return name in {
            'aigc.generate', 'aigc.image', 'aigc.animatic', 'aigc.pack', 'aigc.compose', 'aigc.catalog'} or (
            name == 'aigc.speak' and bool(self.speech_command)) or (
            name == 'aigc.lipsync' and bool(self.lipsync_command))

    def list_tools(self, actor: AuthContext) -> list[ToolSpec]:
        media_id = {'type': 'string', 'minLength': 1, 'maxLength': 128}
        specs: list[tuple[str, str, dict[str, Any], list[str]]] = [
            ('image', '本地分镜图；reference-cleanup 是源图和 mask 的非扩散清理，prompt 仅供审查。', {
                'recipe': {'type': 'string', 'enum': list(IMAGE_RECIPES)},
                'reference_asset_ids': {'type': 'array', 'items': media_id, 'maxItems': 3},
                'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
                'negative': {'type': 'string', 'maxLength': 4000},
                'width': {'type': 'integer', 'minimum': 256, 'maximum': 1024, 'multipleOf': 16},
                'height': {'type': 'integer', 'minimum': 256, 'maximum': 1024, 'multipleOf': 16},
                'steps': {'type': 'integer', 'minimum': 4, 'maximum': 60},
                'seed': {'type': 'integer', 'minimum': 0, 'maximum': 2**63 - 1},
                'image_lora_id': {'type': 'string', 'pattern': r'^[A-Za-z0-9_-]{1,40}$',
                                  'enum': sorted(self.image_loras)},
                'image_lora_strength': {'type': 'number', 'minimum': 0, 'maximum': 1.5},
                'edit_region': {'type': 'array', 'minItems': 4, 'maxItems': 4,
                                'items': {'type': 'number', 'minimum': 0, 'maximum': 1}},
            }, ['recipe', 'prompt']),
            ('generate', '本地 AIGC：参考图片生成桌宠动作或短剧镜头，保留原片、透明帧与配方。', {
                'recipe': {'type': 'string', 'enum': list(RECIPES)},
                'input_asset_id': media_id, 'end_asset_id': media_id,
                'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
                'negative': {'type': 'string', 'maxLength': 4000},
                'width': {'type': 'integer', 'minimum': 256, 'maximum': 1024, 'multipleOf': 16},
                'height': {'type': 'integer', 'minimum': 256, 'maximum': 1024, 'multipleOf': 16},
                'frames': {'type': 'integer', 'minimum': 5, 'maximum': 81},
                'fps': {'type': 'integer', 'minimum': 1, 'maximum': 30},
                'seed': {'type': 'integer', 'minimum': 0, 'maximum': 2**63 - 1},
            }, ['recipe', 'input_asset_id', 'prompt']),
            ('pack', '将桌宠透明帧封装成播放视频，保留原始透明帧。', {
                'input_asset_id': media_id, 'fps': {'type': 'integer', 'minimum': 1, 'maximum': 30},
                'layout': {'type': 'object', 'properties': {
                    k: {'type': 'number', 'minimum': 0, 'maximum': 1024}
                    for k in ('bodyHeight', 'anchorX', 'floor')},
                    'required': ['bodyHeight', 'anchorX', 'floor'], 'additionalProperties': False},
            }, ['input_asset_id']),
            ('compose', '合成短剧镜头、配音与字幕，导出 MP4 和素材说明。', {
                'clip_asset_ids': {'type': 'array', 'items': media_id, 'minItems': 1, 'maxItems': 30},
                'audio_asset_id': media_id,
                'width': {'type': 'integer', 'minimum': 256, 'maximum': 1920, 'multipleOf': 2},
                'height': {'type': 'integer', 'minimum': 256, 'maximum': 1920, 'multipleOf': 2},
                'fps': {'type': 'integer', 'minimum': 1, 'maximum': 30},
                'subtitles': {'type': 'array', 'maxItems': 500, 'items': {'type': 'object',
                    'properties': {'start': {'type': 'number'}, 'end': {'type': 'number'},
                                   'text': {'type': 'string', 'maxLength': 1000}},
                    'required': ['start', 'end', 'text'], 'additionalProperties': False}},
            }, ['clip_asset_ids']),
        ]
        compose_fields = next(fields for name, _, fields, _ in specs if name == 'compose')
        compose_fields.update(
            plan_digest={'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
            clip_edits={'type': 'array', 'maxItems': 30, 'items': {'type': 'object',
                'properties': {'start': {'type': 'number', 'minimum': 0, 'maximum': 60},
                    'duration': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 60},
                    'retime_native': {'type': 'boolean'}},
                'required': ['start', 'duration'], 'additionalProperties': False}},
            dialogue_asset_ids={'type': 'array', 'items': media_id, 'maxItems': 200},
            dialogue_timing={'type': 'array', 'maxItems': 200, 'items': {'type': 'object',
                'properties': {'start': {'type': 'number', 'minimum': 0, 'maximum': 3600},
                    'end': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 3600}},
                'required': ['start', 'end'], 'additionalProperties': False}})
        specs.append(('animatic', '分镜图与分角色配音制作动态粗剪，先审查剧情与节奏。',
                      compose_fields, ['clip_asset_ids', 'clip_edits']))
        if self.lipsync_command:
            whole_timing = compose_fields['dialogue_timing']
            lip_timing = {**whole_timing, 'items': {**whole_timing['items'], 'properties': {
                **whole_timing['items']['properties'],
                'source_start': {'type': 'number', 'minimum': 0, 'maximum': 3600}}}}
            specs.append(('lipsync', '指定说话角色的人脸区域制作本地口型候选，保留原片待审查。', {
                'input_asset_id': media_id, 'start': {'type': 'number', 'minimum': 0, 'maximum': 60},
                'duration': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 60},
                'retime_native': {'type': 'boolean'},
                'fps': {'type': 'integer', 'minimum': 1, 'maximum': 30},
                'face_box': {'type': 'array', 'minItems': 4, 'maxItems': 4,
                             'items': {'type': 'number', 'minimum': 0, 'maximum': 1}},
                'dialogue_asset_ids': compose_fields['dialogue_asset_ids'],
                'dialogue_timing': lip_timing,
            }, ['input_asset_id', 'start', 'duration', 'fps', 'face_box',
                'dialogue_asset_ids', 'dialogue_timing']))
        if self.speech_command:
            specs.append(('speak', '生成本地配音，支持参考声线；通过统一模型调度执行。', {
                'text': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
                'input_asset_id': media_id,
                'emotion': {'type': 'array', 'minItems': 8, 'maxItems': 8,
                            'items': {'type': 'number', 'minimum': 0, 'maximum': 1}},
            }, ['text', 'input_asset_id']))
        result = [ToolSpec(name='aigc.' + name, version='1.0.0', description=label,
            input_schema={'type': 'object', 'properties': fields, 'required': required,
                          'additionalProperties': False},
            risks=['read', 'write', 'exec'], recovery='idempotency_key', timeout_ms=int(self.timeout * 1000))
            for name, label, fields, required in specs]
        image_spec = next(spec for spec in result if spec.name == 'aigc.image')
        image_spec.input_schema['allOf'] = [
            {'if': {'required': ['image_lora_strength']}, 'then': {'required': ['image_lora_id']}},
            {'if': {'required': ['image_lora_id']}, 'then': {'properties': {'recipe': {'const': 'klein-image'}}}},
            {'if': {'required': ['edit_region'], 'not': {'properties': {
                'recipe': {'const': REFERENCE_CLEANUP_RECIPE}}, 'required': ['recipe']}}, 'then': {
                'required': ['reference_asset_ids'],
                'properties': {'reference_asset_ids': {'minItems': 1, 'maxItems': 1}}}},
            {'if': {'properties': {'recipe': {'const': LIGHTNING_RECIPE}}, 'required': ['recipe']},
             'then': {'properties': {'steps': {'const': 4}}}},
            {'if': {'properties': {'recipe': {'const': REFERENCE_CLEANUP_RECIPE}}, 'required': ['recipe']},
             'then': {'required': ['reference_asset_ids', 'edit_region'],
                'properties': {'reference_asset_ids': {'minItems': 2, 'maxItems': 2, 'uniqueItems': True}},
                'not': {'anyOf': [{'required': [field]} for field in (
                    'seed', 'steps', 'negative', 'width', 'height', 'image_lora_id', 'image_lora_strength')]}}},
        ]
        for spec in result:
            if spec.name == 'aigc.lipsync':
                spec.input_schema['allOf'] = [{'if': {'properties': {'retime_native': {'const': True}},
                    'required': ['retime_native']}, 'then': {'properties': {'start': {'const': 0}}}}]
        result.append(ToolSpec(name='aigc.catalog', version='1.0.0', description='本地制作配方与依赖状态',
            input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},
            risks=['read'], recovery='pure', timeout_ms=30000))
        return result

    async def call(self, name: str, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        if not self.has_tool(name):
            raise AgentOSError(ErrorCode.INVALID, 'unknown production tool')
        from jsonschema import ValidationError, validate

        public = {k: v for k, v in args.items() if not k.endswith(('_asset_ref', '_asset_refs'))}
        spec = next(s for s in self.list_tools(ctx.actor) if s.name == name)
        try:
            validate(public, spec.input_schema)
        except ValidationError as exc:
            raise AgentOSError(ErrorCode.INVALID, 'invalid production arguments') from exc
        if args.get('emotion') is not None:
            vector = args['emotion']
            if (not isinstance(vector, list) or len(vector) != 8 or any(
                    type(value) not in (int, float) or not math.isfinite(value) or
                    not 0 <= value <= 1 for value in vector)):
                raise AgentOSError(ErrorCode.INVALID, 'emotion requires eight finite intensities between zero and one')
        if 'edit_region' in args:
            try:
                validate_edit_region(args['edit_region'])
            except ValueError as exc:
                raise AgentOSError(ErrorCode.INVALID, 'invalid normalized image edit region') from exc
        if 'image_lora_strength' in args:
            strength = args['image_lora_strength']
            if type(strength) not in (int, float) or not math.isfinite(strength) or not 0 <= strength <= 1.5:
                raise AgentOSError(ErrorCode.INVALID, 'image LoRA strength must be finite and between zero and 1.5')
        if name == 'aigc.lipsync':
            for cue in args.get('dialogue_timing', []):
                for field in ('start', 'end', 'source_start'):
                    if field not in cue:
                        continue
                    value = cue[field]
                    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 3600:
                        raise AgentOSError(ErrorCode.INVALID, 'speech timing must be finite and nonnegative')
        if name == 'aigc.catalog':
            return {'status': 'completed', 'value': {'recipes': RECIPES, 'image_recipes': IMAGE_RECIPES,
                'speech_enabled': bool(self.speech_command), 'lipsync_enabled': bool(self.lipsync_command),
                'scheduler_required': True}}
        remaining = ((ctx.deadline_at - utc_now()).total_seconds() if ctx.deadline_at else self.timeout)
        if remaining <= 0:
            raise AgentOSError(ErrorCode.TIMEOUT, 'production deadline expired')
        return await asyncio.to_thread(self._execute, name, args, ctx.idempotency_key, min(remaining, self.timeout))

    def _execute(self, name: str, args: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
        job_id = hashlib.sha256((name + '\0' + key).encode()).hexdigest()
        with self._lock:
            lock = self._locks.setdefault(job_id, threading.Lock())
        with lock:
            return self._execute_locked(name, args, job_id, timeout)

    def _execute_locked(self, name: str, args: dict[str, Any], job_id: str, timeout: float) -> dict[str, Any]:
        if name == 'aigc.generate' and bool(args.get('end_asset_id')) != bool(args.get('end_asset_ref')):
            raise AgentOSError(ErrorCode.INVALID, 'end image transfer is incomplete')
        if (name == 'aigc.image' and
                len(args.get('reference_asset_ids', [])) != len(args.get('reference_asset_refs', []))):
            raise AgentOSError(ErrorCode.INVALID, 'reference image transfer is incomplete')
        lora = self._image_lora(name, args)
        preset = self._recipe_lora(name, args)
        model = self._recipe_model(name, args)
        lora_name = lora[0] if lora else preset[0] if preset else None
        refs = [str(args[k]) for k in ('input_asset_ref', 'end_asset_ref', 'audio_asset_ref') if args.get(k)]
        for field in ('clip_asset_refs', 'reference_asset_refs', 'dialogue_asset_refs'):
            refs += [str(x) for x in args.get(field, [])]
        inputs = [asset_file(self.assets, ref) for ref in refs]
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs]
        cleanup = self._cleanup_geometry(inputs, args) if model else None
        normalized = {k: v for k, v in args.items() if not k.endswith(('_asset_id', '_asset_ids',
                                                                      '_asset_ref', '_asset_refs'))}
        if lora:
            normalized.update(image_lora_name=lora[0], image_lora_sha256=lora[1])
            normalized.setdefault('image_lora_strength', 1)
        if preset:
            normalized.update(recipe_lora_name=preset[0], recipe_lora_sha256=preset[1])
        if model:
            normalized.update(recipe_model_name=model[0], recipe_model_sha256=model[1],
                              native_version=REFERENCE_CLEANUP_VERSION, operation='reference-preparation')
        if name == 'aigc.image' and ('edit_region' in args or preset or model):
            graph = image_graph(args, [f'reference-{index}.png' for index in
                                      range(len(args.get('reference_asset_refs', [])))],
                                'aigc/fingerprint', lora_name=lora_name, cleanup=cleanup)
            normalized['image_graph_sha256'] = hashlib.sha256(json.dumps(
                graph, sort_keys=True, allow_nan=False).encode()).hexdigest()
            if 'edit_region' in args:
                normalized['edit_region'] = validate_edit_region(args['edit_region'])
        signature = (fingerprint(normalized, hashes) if name in {'aigc.generate', 'aigc.image'} else
                     hashlib.sha256(json.dumps([name, normalized, hashes], sort_keys=True,
                                              allow_nan=False).encode()).hexdigest())
        work = self.jobs / job_id
        work.mkdir(parents=True, exist_ok=True)
        record = work / 'job.json'
        state: dict[str, Any] = json.loads(record.read_text()) if record.exists() else {
            'job_id': job_id, 'fingerprint': signature, 'state': 'prepared',
            'prompt_id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'agentos-aigc:' + job_id)),
        }
        if state['fingerprint'] != signature:
            raise AgentOSError(ErrorCode.CONFLICT, 'production key reused with changed inputs')
        if state.get('result'):
            for entry in state['result']['assets']:
                path = asset_file(self.assets, entry['asset_ref'])
                if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                    raise AgentOSError(ErrorCode.UNCERTAIN, 'completed media failed integrity verification')
            return {'status': 'completed', 'value': {**state['result'], 'reused': True}}
        save_json(record, state)
        if name in {'aigc.generate', 'aigc.image'}:
            result = self._generate(args, work, state, timeout, still=name == 'aigc.image',
                                    lora_name=lora_name, cleanup=cleanup, input_hashes=hashes if cleanup else None)

        else:
            result = self._process(name, args, work, timeout)
        if model:
            result.update(operation='reference-preparation', source_reference_only=True, text_conditioning=False)
        metadata = work / 'production.json'
        save_json(metadata, {'tool': name, 'recipe_version': 1, 'parameters': normalized,
            'input_sha256': hashes, 'visual_review': 'pending', 'user_accepted': False,
            'assets': result['assets']})
        result['assets'].append(register_output(metadata, self.assets, 'production_record'))
        result.update(job_id=job_id, visual_review='pending', reused=False)
        state.update(state='completed', result=result)
        save_json(record, state)
        return {'status': 'completed', 'value': result}

    def _image_lora(self, name: str, args: dict[str, Any]) -> tuple[str, str] | None:
        id = args.get('image_lora_id')
        if id is None:
            if 'image_lora_strength' in args:
                raise AgentOSError(ErrorCode.INVALID, 'image LoRA strength requires a registered ID')
            return None
        strength = args.get('image_lora_strength', 1)
        if (name != 'aigc.image' or args.get('recipe', 'klein-image') != 'klein-image' or
                type(strength) not in (int, float) or not math.isfinite(strength) or not 0 <= strength <= 1.5):
            raise AgentOSError(ErrorCode.INVALID, 'registered image LoRA requires Klein and a finite strength')
        if not isinstance(id, str) or id not in self.image_loras:
            raise AgentOSError(ErrorCode.INVALID, 'unknown registered image LoRA')
        entry = self.image_loras[id]
        if (not isinstance(entry, dict) or set(entry) != {'path', 'sha256'} or
                not isinstance(entry['path'], str) or not isinstance(entry['sha256'], str) or
                not re.fullmatch(r'[a-f0-9]{64}', entry['sha256'])):
            raise AgentOSError(ErrorCode.INVALID, 'invalid registered image LoRA configuration')
        path = Path(entry['path']).resolve()
        if (not path.is_file() or path.parent.name.casefold() != 'loras' or path.suffix.casefold() != '.safetensors' or
                any(char in path.name for char in '/\\:')):
            raise AgentOSError(ErrorCode.UNAVAILABLE, 'registered image LoRA is not available in the model directory')
        try:
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        except OSError as exc:
            raise AgentOSError(ErrorCode.UNAVAILABLE, 'registered image LoRA could not be verified') from exc
        if actual != entry['sha256']:
            raise AgentOSError(ErrorCode.UNCERTAIN, 'registered image LoRA failed integrity verification')
        return path.name, actual

    def _recipe_lora(self, name: str, args: dict[str, Any]) -> tuple[str, str] | None:
        recipe = args.get('recipe')
        if name != 'aigc.image' or recipe != LIGHTNING_RECIPE:
            return None
        registered = self.recipe_loras.get(recipe)
        if registered is None:
            raise AgentOSError(ErrorCode.UNAVAILABLE, 'the official recipe LoRA must be registered first')
        if (not isinstance(registered, dict) or set(registered) != {'path', 'sha256'}
                or not isinstance(registered['path'], str) or not isinstance(registered['sha256'], str)):
            raise AgentOSError(ErrorCode.INVALID, 'invalid registered recipe LoRA configuration')
        pin = RECIPE_LORAS[recipe]
        if registered['sha256'] != pin['sha256']:
            raise AgentOSError(ErrorCode.CONFLICT, 'registered recipe LoRA differs from the authorized version')
        path = Path(registered['path']).resolve()
        if not path.is_file() or path.parent.name.casefold() != 'loras' or path.name != pin['filename']:
            raise AgentOSError(ErrorCode.UNAVAILABLE, 'registered recipe LoRA is unavailable in the model directory')
        try:
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        except OSError as exc:
            raise AgentOSError(ErrorCode.UNAVAILABLE, 'registered recipe LoRA could not be verified') from exc
        if actual != pin['sha256']:
            raise AgentOSError(ErrorCode.UNCERTAIN, 'registered recipe LoRA failed integrity verification')
        return path.name, actual

    def _recipe_model(self, name: str, args: dict[str, Any]) -> tuple[str, str] | None:
        if name != 'aigc.image' or args.get('recipe') != REFERENCE_CLEANUP_RECIPE:
            return None
        entry = self.recipe_models.get(REFERENCE_CLEANUP_RECIPE)
        if entry is None:
            raise AgentOSError(ErrorCode.UNAVAILABLE, 'reference cleanup requires its registered CNN model')
        if (not isinstance(entry, dict) or set(entry) != {'path', 'sha256'} or
                not isinstance(entry['path'], str) or not isinstance(entry['sha256'], str)):
            raise AgentOSError(ErrorCode.INVALID, 'invalid registered recipe model configuration')
        if entry['sha256'] != REFERENCE_CLEANUP_MODEL['sha256']:
            raise AgentOSError(ErrorCode.CONFLICT, 'registered cleanup model differs from the pinned preset')
        path = Path(entry['path']).resolve()
        if (not path.is_file() or path.parent.name.casefold() != 'inpaint' or
                path.name != REFERENCE_CLEANUP_MODEL['filename']):
            raise AgentOSError(ErrorCode.UNAVAILABLE,
                               'registered cleanup model is unavailable in the inpaint directory')
        try:
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        except OSError as exc:
            raise AgentOSError(ErrorCode.UNAVAILABLE, 'registered cleanup model could not be verified') from exc
        if actual != entry['sha256']:
            raise AgentOSError(ErrorCode.UNCERTAIN, 'registered cleanup model failed integrity verification')
        return path.name, actual

    @staticmethod
    def _cleanup_geometry(inputs: list[Path], args: dict[str, Any]) -> dict[str, Any]:
        if len(inputs) != 2 or len(args.get('reference_asset_ids', [])) != 2:
            raise AgentOSError(ErrorCode.INVALID, 'cleanup requires two ordered source and mask images')
        try:
            region = validate_edit_region(args.get('edit_region'))
            with Image.open(io.BytesIO(inputs[0].read_bytes())) as source, Image.open(
                    io.BytesIO(inputs[1].read_bytes())) as mask:
                for image in (source, mask):
                    if (image.format != 'PNG' or getattr(image, 'n_frames', 1) != 1 or
                            image.getexif().get(274, 1) != 1 or
                            image.info.get('transparency') is not None or
                            image.mode not in {'L', 'RGB', 'RGBA'} or
                            ('A' in image.getbands() and image.getchannel('A').getextrema() != (255, 255))):
                        raise ValueError('opaque, oriented single PNG inputs required')
                width, height = source.size
                if (mask.size != source.size or min(width, height) < 16 or max(width, height) > 1920):
                    raise ValueError('source and mask require matching bounded original dimensions')
                rgb = mask.convert('RGB')
                red, green, blue = rgb.split()
                colors = red.getcolors(3)
                if (not colors or any(color not in (0, 255) for _, color in colors) or
                        ImageChops.difference(red, green).getbbox() or ImageChops.difference(red, blue).getbbox()):
                    raise ValueError('mask must contain only black and white pixels')
                left, top, right, bottom = [round(point * extent) for point, extent in
                    zip(region, [width, height, width, height], strict=True)]
                if left >= right or top >= bottom:
                    raise ValueError('context has no source pixels')
                bbox = red.getbbox()
                if bbox and not (left <= bbox[0] and top <= bbox[1] and right >= bbox[2] and bottom >= bbox[3]):
                    raise ValueError('context does not contain every edited mask pixel')
                return {'crop': [left, top, right - left, bottom - top], 'empty': bbox is None}
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise AgentOSError(ErrorCode.INVALID,
                               'cleanup needs matching opaque binary-mask PNGs inside its context') from exc

    def _generate(self, args: dict[str, Any], work: Path, state: dict[str, Any], timeout: float,
                  *, still: bool = False, lora_name: str | None = None,
                  cleanup: dict[str, Any] | None = None, input_hashes: list[str] | None = None) -> dict[str, Any]:
        sources = ([asset_file(self.assets, str(ref)) for ref in args.get('reference_asset_refs', [])]
                   if still else [asset_file(self.assets, str(args.get('input_asset_ref', '')))])
        if not still:
            if bool(args.get('end_asset_id')) != bool(args.get('end_asset_ref')):
                raise AgentOSError(ErrorCode.INVALID, 'end image transfer is incomplete')
            if args.get('end_asset_ref'):
                sources.append(asset_file(self.assets, str(args['end_asset_ref'])))
        if still and len(sources) != len(args.get('reference_asset_ids', [])):
            raise AgentOSError(ErrorCode.INVALID, 'reference transfer is incomplete')
        graph_file = work / 'workflow.json'
        if graph_file.exists():
            graph = json.loads(graph_file.read_text())
            if still and ('edit_region' in args or args.get('recipe') == LIGHTNING_RECIPE or cleanup):
                names = ['aigc-' + state['job_id'][:24] + f'-{i}.png' for i in range(len(sources))]
                expected_graph = image_graph(args, names, 'aigc/' + state['job_id'],
                                             lora_name=lora_name, cleanup=cleanup)
                if json.dumps(graph, sort_keys=True, allow_nan=False) != json.dumps(
                        expected_graph, sort_keys=True, allow_nan=False):
                    raise AgentOSError(ErrorCode.UNCERTAIN, 'persisted production graph failed integrity verification')
        else:
            width = args.get('width', 768 if still else 512)
            height = args.get('height', 1024 if still else 800)
            prefix = 'aigc/' + state['job_id']
            if still:
                names = ['aigc-' + state['job_id'][:24] + f'-{i}.png' for i in range(len(sources))]
                graph = image_graph(args, names, prefix, lora_name=lora_name, cleanup=cleanup)
                for index, source in enumerate(sources):
                    if cleanup is not None:
                        shutil.copyfile(source, work / f'prepared-{index}.png')
                    elif args.get('recipe') == 'klein-image':
                        prepare_reference(source, work / f'prepared-{index}.png', width, height, max_side=1024)
                    elif args.get('recipe') == 'qwen-style-v2':
                        prepare_reference(source, work / f'prepared-{index}.png', width, height)
                    else:
                        prepare_image(self.ffmpeg, source, work / f'prepared-{index}.png', width, height)
            else:
                end_name = 'aigc-' + state['job_id'][:24] + '-end.png' if len(sources) == 2 else None
                graph = recipe_graph(args, 'aigc-' + state['job_id'][:24] + '.png', prefix,
                                     end_image_name=end_name)
                prepare_image(self.ffmpeg, sources[0], work / 'prepared.png', width, height)
                if end_name:
                    prepare_image(self.ffmpeg, sources[1], work / 'prepared-end.png', width, height)
            save_json(graph_file, graph)
        if cleanup is not None:
            for index in range(len(sources)):
                prepared = work / f'prepared-{index}.png'
                if input_hashes is None or hashlib.sha256(prepared.read_bytes()).hexdigest() != input_hashes[index]:
                    raise AgentOSError(ErrorCode.UNCERTAIN,
                                       'prepared cleanup source or mask failed integrity verification')
        deadline = time.monotonic() + timeout
        with httpx.Client(timeout=60, trust_env=False) as client:
            discovery = self._request(client, 'GET', '/.well-known/local-model-scheduler').json()
            if discovery.get('service') != 'local-model-scheduler':
                raise AgentOSError(ErrorCode.DENIED, 'production requires the managed ComfyUI endpoint')
            pid = state['prompt_id']
            history = self._request(client, 'GET', '/history/' + pid).json().get(pid)
            if not history:
                # Stable UUID and persisted graph let the scheduler deduplicate ambiguous submissions.
                if state['state'] == 'prepared':
                    info = self._request(client, 'GET', '/object_info').json()
                    if any(node['class_type'] not in info for node in graph.values()):
                        raise AgentOSError(ErrorCode.INVALID, 'production recipe has missing nodes')
                    uploads = [(node['inputs']['image'], work / (f'prepared-{index}.png' if still else
                               'prepared.png' if index == 0 else 'prepared-end.png'))
                               for index, node in enumerate(
                                   n for n in graph.values() if n['class_type'] == 'LoadImage')]
                    for image_name, prepared in uploads:
                        with prepared.open('rb') as image:
                            self._request(client, 'POST', '/upload/image',
                                files={'image': (image_name, image, 'image/png')}, data={'overwrite': 'true'})
                    state['state'] = 'submitting'
                    save_json(work / 'job.json', state)
                submission = self._request(client, 'POST', '/prompt', json={
                    'prompt_id': pid, 'prompt': graph, 'client_id': 'agentos-aigc',
                    'extra_data': {'aigc_job': state['job_id']}}).json()
                if submission.get('prompt_id') != pid:
                    raise AgentOSError(ErrorCode.UNCERTAIN, 'scheduler submission could not be reconciled')
                state['state'] = 'submitted'
                save_json(work / 'job.json', state)
            while True:
                history = self._request(client, 'GET', '/history/' + pid).json().get(pid)
                if history and history.get('status', {}).get('status_str') == 'error':
                    state['state'] = 'failed'
                    save_json(work / 'job.json', state)
                    raise AgentOSError(ErrorCode.INVALID, 'managed generation failed; inspect this attempt')
                if history and history.get('status', {}).get('completed'):
                    break
                if time.monotonic() >= deadline:
                    raise AgentOSError(ErrorCode.TIMEOUT, 'generation still pending; resume the same production key')
                time.sleep(2)
            assets = []
            outputs = history['outputs']
            raw = self._files(outputs.get('16', {}))
            if not raw:
                raise AgentOSError(ErrorCode.UNCERTAIN, 'completed generation has no primary media')
            movie = work / ('keyframe' if still else 'raw')
            movie = movie.with_suffix(Path(raw[0]['filename']).suffix)
            movie.write_bytes(self._download(client, raw[0]))
            primary = register_output(movie, self.assets, 'keyframe' if still else 'raw_video')
            assets.append(primary)
            alpha = self._files(outputs.get('24', {}))
            if alpha:
                archive = work / 'alpha-frames.zip'
                with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
                    for index, item in enumerate(alpha):
                        z.writestr(f'{index:04d}.png', self._download(client, item))
                assets.append(register_output(archive, self.assets, 'alpha_frames'))
            workflow = register_output(work / 'workflow.json', self.assets, 'comfy_workflow')
            assets.append(workflow)
            return {**primary, 'assets': assets, 'frames': 1 if still else args.get('frames', 65),
                    'fps': None if still else args.get('fps', 16),
                    'prompt_id': pid, 'recipe': args.get('recipe', 'story-shot')}

    @staticmethod
    def _files(output: dict[str, Any]) -> list[dict[str, str]]:
        return [entry for key in ('images', 'videos', 'gifs') for entry in output.get(key, [])
                if isinstance(entry, dict) and entry.get('filename')]

    def _request(self, client: httpx.Client, method: str, suffix: str, **kwargs: Any) -> httpx.Response:
        try:
            response = client.request(method, self.base + suffix, **kwargs)
        except httpx.HTTPError as exc:
            raise AgentOSError(ErrorCode.UNAVAILABLE, 'managed generation connection lost; reconcile same job') from exc
        if not response.is_success:
            raise AgentOSError(ErrorCode.INVALID if response.status_code < 500 else ErrorCode.UNAVAILABLE,
                               f'managed media request failed: HTTP {response.status_code}')
        return response

    def _download(self, client: httpx.Client, item: dict[str, str]) -> bytes:
        data = self._request(client, 'GET', '/view', params={
            'filename': item['filename'], 'subfolder': item.get('subfolder', ''),
            'type': item.get('type', 'output')}).content
        if len(data) > MAX_BYTES:
            raise AgentOSError(ErrorCode.INVALID, 'output exceeds media size limit')
        return data

    def _process(self, name: str, args: dict[str, Any], work: Path, timeout: float) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        assets: list[dict[str, Any]] = []
        if name == 'aigc.pack':
            output = work / 'playback.mp4'
            extra = pack_alpha(self.ffmpeg, asset_file(self.assets, str(args['input_asset_ref'])),
                               work, int(args.get('fps', 16)), output)
            layout = args.get('layout')
            if layout is not None:
                if (not 0 < layout['bodyHeight'] <= extra['height']
                        or not 0 <= layout['anchorX'] <= extra['width']
                        or not 0 < layout['floor'] <= extra['height']):
                    raise AgentOSError(ErrorCode.INVALID, 'fixed character layout exceeds media bounds')
                extra.update(layout)
            extra['position_source'] = ('fixed_character_layout' if layout is not None else
                                        'foreground_union_pending_review')
        elif name in {'aigc.compose', 'aigc.animatic'}:
            output = work / 'film.mp4'
            audio = asset_file(self.assets, str(args['audio_asset_ref'])) if args.get('audio_asset_ref') else None
            extra['duration'] = compose(self.ffmpeg, [asset_file(self.assets, str(x)) for x in args['clip_asset_refs']],
                    work, output, width=int(args.get('width', 512)), height=int(args.get('height', 800)),
                    fps=int(args.get('fps', 16)), audio=audio, subtitles=args.get('subtitles'),
                    edits=args.get('clip_edits'), still=name == 'aigc.animatic',
                    dialogue=[asset_file(self.assets, str(x)) for x in args.get('dialogue_asset_refs', [])],
                    timing=args.get('dialogue_timing'))
            if (work / 'subtitles.srt').exists():
                assets.append(register_output(work / 'subtitles.srt', self.assets, 'subtitles'))
        elif name == 'aigc.lipsync':
            if not self.lipsync_command:
                raise AgentOSError(ErrorCode.UNAVAILABLE, 'local lip sync is not configured')
            x1, y1, x2, y2 = args['face_box']
            if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
                raise AgentOSError(ErrorCode.INVALID, 'invalid speaker face region')
            source = asset_file(self.assets, str(args['input_asset_ref']))
            start, duration = float(args['start']), float(args['duration'])
            retime = args.get('retime_native', False)
            if type(retime) is not bool or retime and start != 0:
                raise AgentOSError(ErrorCode.INVALID, 'whole native retiming requires zero trim')
            if not retime and start + duration > probe_duration(self.ffmpeg, source) + 1 / int(args['fps']):
                raise AgentOSError(ErrorCode.INVALID, 'lip-sync edit exceeds native video')
            video, audio, output = work / 'lip-input.mp4', work / 'lip-audio.wav', work / 'lipsync.mp4'
            if retime:
                normalize_clip(self.ffmpeg, source, video, fps=int(args['fps']), start=start,
                               duration=duration, retime_native=True)
            else:
                ffmpeg_run(self.ffmpeg, ['-ss', str(start), '-i', str(source), '-t', str(duration), '-an',
                    '-vf', 'fps=25', '-c:v', 'libx264', '-crf', '16', '-pix_fmt', 'yuv420p', str(video)])
            mixed = mix_dialogue(self.ffmpeg, work, duration,
                [asset_file(self.assets, str(x)) for x in args['dialogue_asset_refs']], args['dialogue_timing'])
            mixed.replace(audio)
            request = work / 'lipsync-request.json'
            save_json(request, {'video': str(video), 'audio': str(audio), 'output': str(output),
                                'face_box': args['face_box'], 'fps': args['fps'],
                                'active': args['dialogue_timing']})
            try:
                process = subprocess.run([*self.lipsync_command, str(request)], capture_output=True, timeout=timeout)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AgentOSError(ErrorCode.UNCERTAIN, 'local lip sync pending; reconcile before retrying') from exc
            if process.returncode or not output.is_file():
                raise AgentOSError(ErrorCode.UNAVAILABLE, 'local lip sync failed; diagnostics remain private')
            if abs(probe_duration(self.ffmpeg, output)-duration) > 1 / int(args['fps']):
                raise AgentOSError(ErrorCode.INVALID, 'lip-sync output changed clip timing')
            extra['duration'] = duration
        else:
            if not self.speech_command:
                raise AgentOSError(ErrorCode.UNAVAILABLE, 'local speech is not configured')
            output = work / 'speech.wav'
            request = work / 'speech-request.json'
            speech = {'text': args['text'],
                'reference': str(asset_file(self.assets, str(args['input_asset_ref']))), 'output': str(output)}
            if args.get('emotion') is not None:
                speech['emotion'] = args['emotion']
            save_json(request, speech)
            try:
                process = subprocess.run([*self.speech_command, str(request)], capture_output=True, timeout=timeout)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AgentOSError(ErrorCode.UNCERTAIN, 'local speech pending; reconcile before retrying') from exc
            if process.returncode or not output.is_file():
                raise AgentOSError(ErrorCode.UNAVAILABLE, 'local speech failed; model diagnostics remain private')
        primary = register_output(output, self.assets, 'playback_video' if name == 'aigc.pack' else
                                  'film' if name == 'aigc.compose' else
                                  'animatic' if name == 'aigc.animatic' else
                                  'lipsync_video' if name == 'aigc.lipsync' else 'speech')
        assets.insert(0, primary)
        if name == 'aigc.pack':
            manifest = work / 'manifest.json'
            mask = register_output(work / 'mask.png', self.assets, 'pet_mask')
            assets.append(mask)
            save_json(manifest, {**extra,
                'clips': {'motion': {'url': 'playback.mp4', 'mask': 'mask.png', 'fps': extra['fps'],
                    'frames': extra['frames'], 'duration': extra['duration'], 'label': '生成动作'}},
                'user_accepted': False})
            assets.append(register_output(manifest, self.assets, 'pet_manifest'))
            bundle = work / 'pet-bundle.zip'
            with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in (output, work / 'mask.png', manifest):
                    archive.write(path, path.name)
            assets.append(register_output(bundle, self.assets, 'pet_bundle'))
        if name != 'aigc.speak':
            sheet = work / 'preview.jpg'
            ffmpeg_run(self.ffmpeg, ['-i', str(output), '-vf', 'thumbnail,scale=320:-1',
                                    '-frames:v', '1', str(sheet)])
            assets.append(register_output(sheet, self.assets, 'preview'))
        return {**primary, 'assets': assets, **extra}
