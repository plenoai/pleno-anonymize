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
      '@scores-en': path.resolve(__dirname, '../packages/training/output/en-transformer/scores.json'),
      '@scores-en-cnn': path.resolve(__dirname, '../packages/training/output/en/scores.json'),
      '@external-scores-ja': path.resolve(__dirname, '../packages/training/output/ja/external_scores.json'),
      '@external-scores-en': path.resolve(__dirname, '../packages/training/output/en/external_scores.json'),
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
