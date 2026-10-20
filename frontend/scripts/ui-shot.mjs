#!/usr/bin/env node
/**
 * 用 Chrome DevTools Protocol 给页面拍快照（不装 puppeteer，Node 22+ 自带 WebSocket）
 *
 * 用途：验收"进度条随真实步骤变化"——对同一个页面实例在不同时刻各拍一张，
 * 对比 PNG 的同时也能拿到控制台错误数，避免"看起来有图其实白屏"。
 *
 * 用法：
 *   1) 先起一个带调试端口的 headless Edge/Chrome：
 *        msedge --headless=new --remote-debugging-port=9222 --user-data-dir=<tmp> about:blank
 *   2) node scripts/ui-shot.mjs <url> <输出前缀> [毫秒,毫秒,...]
 *
 * 例：node scripts/ui-shot.mjs http://localhost:5300/task/xxx ./out/task 6000,22000
 */

import { writeFileSync } from 'node:fs'
import { setTimeout as sleep } from 'node:timers/promises'

const CDP = process.env.CDP_URL || 'http://127.0.0.1:9222'

const [, , url, outPrefix = './shot', delaysArg = '6000'] = process.argv
if (!url) {
  console.error('用法: node scripts/ui-shot.mjs <url> <输出前缀> [延迟列表]')
  process.exit(2)
}

const delays = delaysArg
  .split(',')
  .map((value) => Number(value.trim()))
  .filter((value) => Number.isFinite(value) && value >= 0)
  .sort((a, b) => a - b)

const targets = await (await fetch(`${CDP}/json/list`)).json()
const page = targets.find((target) => target.type === 'page')
if (!page) {
  console.error('没找到可用的 page target')
  process.exit(3)
}

const socket = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true })
  socket.addEventListener('error', reject, { once: true })
})

let nextId = 0
const pending = new Map()
const consoleErrors = []
const consoleMessages = []

socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data)

  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id)
    pending.delete(message.id)
    if (message.error) reject(new Error(JSON.stringify(message.error)))
    else resolve(message.result)
    return
  }

  if (message.method === 'Runtime.consoleAPICalled') {
    const text = (message.params.args || [])
      .map((arg) => arg.value ?? arg.description ?? '')
      .join(' ')
    consoleMessages.push(`${message.params.type}: ${text}`)
    if (message.params.type === 'error') consoleErrors.push(text)
  }

  if (message.method === 'Runtime.exceptionThrown') {
    const detail = message.params.exceptionDetails
    consoleErrors.push(detail.exception?.description || detail.text || 'unknown exception')
  }
})

function send(method, params = {}) {
  const id = ++nextId
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject })
    socket.send(JSON.stringify({ id, method, params }))
  })
}

await send('Page.enable')
await send('Runtime.enable')
await send('Log.enable')
// 给一个足够大的视口，否则表格/时间线会被截在首屏之外
await send('Emulation.setDeviceMetricsOverride', {
  width: 1280,
  height: 1000,
  deviceScaleFactor: 1,
  mobile: false
})
await send('Page.navigate', { url })

await sleep(1200)

const shots = []
let previous = 0
for (const delay of delays) {
  await sleep(Math.max(0, delay - previous))
  previous = delay

  const { data } = await send('Page.captureScreenshot', { format: 'png' })
  const path = `${outPrefix}-${delay}ms.png`
  writeFileSync(path, Buffer.from(data, 'base64'))

  // 顺便把页面上的关键文本抓出来，作为"内容真的渲染了"的证据
  const { result } = await send('Runtime.evaluate', {
    expression: `(() => {
      const text = document.body.innerText || ''
      return JSON.stringify({
        title: document.title,
        progress: (document.querySelector('.ant-progress-text') || {}).innerText || null,
        statusTag: (document.querySelector('.status-tag') || {}).innerText || null,
        steps: Array.from(document.querySelectorAll('.step-title')).map(el => el.innerText.trim()),
        hasPlanCard: document.body.innerHTML.includes('行程概览'),
        snippet: text.slice(0, 160)
      })
    })()`,
    returnByValue: true
  })

  let parsed = null
  try {
    parsed = JSON.parse(result.value)
  } catch {
    parsed = { raw: result.value }
  }

  shots.push({ delay, path, ...parsed })
}

socket.close()

console.log(
  JSON.stringify(
    {
      url,
      shots,
      consoleErrors,
      consoleMessageCount: consoleMessages.length
    },
    null,
    2
  )
)

if (consoleErrors.length) process.exit(1)
