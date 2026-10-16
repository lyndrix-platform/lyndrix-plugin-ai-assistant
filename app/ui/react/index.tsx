import PluginApp from './PluginApp'

// Explicit global assignment — the shell (lyndrix-ui usePluginModules) reads
// window.__lyndrix_plugin_<safe id>, safe id = plugin id with [.-] → "_".
;(window as unknown as Record<string, unknown>)['__lyndrix_plugin_lyndrix_plugin_ai_assistant'] = {
  PluginApp,
  // Mirrors manifest.react_routes: the chat is a sidebar tab, the settings
  // page is reached through the gear on the Plugins page (settings_ui_route)
  // and the header of the chat. The floating panel "chat" reuses PluginApp.
  pluginRoutes: [
    { path: '/ai/chat', label: 'KI-Assistent', icon: 'smart_toy', sidebar_visible: true },
    { path: '/ai/settings', label: 'KI-Assistent – Einstellungen', icon: 'settings', sidebar_visible: false },
  ],
}
