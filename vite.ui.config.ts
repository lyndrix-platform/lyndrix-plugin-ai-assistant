import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'path'

/**
 * Builds the federated React UI (IIFE) the lyndrix-ui shell loads from
 * /api/plugins/lyndrix.plugin.ai_assistant/static/ui_bundle.js — both for the
 * /ai/chat and /ai/settings routes and for the floating "chat" panel.
 *
 * The classic JSX runtime is required: the automatic runtime would import
 * react/jsx-runtime, which is not in the externals map and would bundle a
 * second React next to the host's (window.__lyndrix_react).
 */
export default defineConfig({
  plugins: [react({ jsxRuntime: 'classic' })],
  build: {
    lib: {
      entry: resolve(__dirname, 'app/ui/react/index.tsx'),
      name: '__lyndrix_plugin_lyndrix_plugin_ai_assistant',
      formats: ['iife'],
      fileName: () => 'ui_bundle.js',
    },
    outDir: 'app/ui/static',
    emptyOutDir: true,
    rollupOptions: {
      // Shared from the host shell via window globals — never bundled.
      external: ['react', 'react-dom', 'react-dom/client', 'react-i18next', 'i18next', '@lyndrix/ui'],
      output: {
        globals: {
          react: '__lyndrix_react',
          'react-dom': '__lyndrix_react',
          'react-dom/client': '__lyndrix_react_dom_client',
          'react-i18next': '__lyndrix_react_i18next',
          i18next: '__lyndrix_i18n',
          '@lyndrix/ui': '__lyndrix_ui',
        },
      },
    },
  },
})
