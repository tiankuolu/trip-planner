/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端地址；留空则走 Vite 代理 /api */
  readonly VITE_API_BASE_URL?: string
  /** 高德 Web 服务 Key（前端不直接用，走后端） */
  readonly VITE_AMAP_WEB_KEY?: string
  /** 高德 Web 端 JS API Key（地图渲染用） */
  readonly VITE_AMAP_WEB_JS_KEY?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'

  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}
