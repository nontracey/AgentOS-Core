import { defineConfig } from 'vite';
export default defineConfig({
  define: {'process.env.NODE_ENV': JSON.stringify('production')},
  build: {
    minify: true, outDir: '../workflow-dist', emptyOutDir: true,
    lib: {entry:'src/editor.tsx', formats:['es'], fileName:()=> 'editor.js'},
    rollupOptions: {output:{assetFileNames:'editor.[ext]'}},
  },
});
