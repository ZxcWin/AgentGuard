// Optional local preview evidence. Run while tools/dev.py is running.
import { chromium } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'

const origin = new URL(process.argv[2] || 'http://127.0.0.1:5173/')
if (!['http:', 'https:'].includes(origin.protocol) || origin.username || origin.password) throw new Error('Expected HTTP(S) console URL without credentials')
const directory = fileURLToPath(new URL('../../.agentguard/preview/', import.meta.url))
await mkdir(directory, { recursive: true })
const browser = await chromium.launch()
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  for (const [route, title] of [['', '运行概览'], ['policies', '防护策略'], ['audit', '审计记录'], ['billing', '成本账本']]) {
    const url = new URL(origin); url.hash = '/' + route
    await page.goto(url.href)
    await page.getByRole('heading', { name: title, exact: true }).waitFor()
    await page.locator('.resource-state').filter({ hasText: '正在读取运行数据' }).waitFor({ state: 'hidden' })
    const failure = page.locator('.resource-state[role="alert"]')
    if (await failure.count()) throw new Error(await failure.innerText())
    await page.screenshot({ path: join(directory, `${route || 'overview'}.png`) })
    if (route === 'policies') {
      await page.locator('.policy-card').filter({ has: page.getByRole('heading', { name: '故障熔断', exact: true }) }).screenshot({ path: join(directory, 'circuit-policy.png') })
      await page.locator('.policy-card').filter({ has: page.getByRole('heading', { name: '响应质量', exact: true }) }).screenshot({ path: join(directory, 'quality-policy.png') })
    }
  }
  if (errors.length) throw new Error(errors.join('\n'))
  console.log(`Preview screenshots saved: ${directory}`)
} finally { await browser.close() }
