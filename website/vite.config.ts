import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/pleno-anonymize/',
  resolve: {
    alias: {
      '@scores': path.resolve(__dirname, '../packages/training/output/scores.json'),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'https://anonymize.plenoai.com',
        changeOrigin: true,
      },
    },
  },
})
