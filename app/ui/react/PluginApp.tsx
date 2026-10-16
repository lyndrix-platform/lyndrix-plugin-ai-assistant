import React from 'react'
// react-i18next is provided by the host shell (window.__lyndrix_react_i18next);
// strings come from locales/ai_assistant.<locale>.json (namespace
// "ai_assistant"), auto-registered by the core and served via its catalog.
import ChatView from './ChatView'
import SettingsView from './SettingsView'
import { AiStyles, useLocation } from './shared'

/** Set by the shell when the plugin is hosted inside a floating panel
 *  (manifest.panels → <PluginApp panel={{ id, pluginId }} />). */
export interface PanelProps {
  id: string
  pluginId: string
}

/** Root component.
 *  - Panel mode (`panel` prop): always the chat, compact — the panel has its
 *    own title bar, so no page header; the session list folds into a picker.
 *  - Route mode: /apps/<id>/ai/settings → settings, everything else → chat. */
export default function PluginApp({ panel }: { panel?: PanelProps }) {
  const { pathname } = useLocation()
  if (panel) {
    return (
      <>
        <AiStyles />
        <ChatView compact />
      </>
    )
  }
  const isSettings = pathname.replace(/\/+$/, '').endsWith('/settings')
  return (
    <>
      <AiStyles />
      {isSettings ? <SettingsView /> : <ChatView />}
    </>
  )
}
