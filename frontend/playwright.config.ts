import { defineConfig, devices } from '@playwright/test'
export default defineConfig({
  testDir: './e2e', fullyParallel: false, workers: 1,
  use: { baseURL: 'http://127.0.0.1:8001', trace: 'retain-on-failure', ...devices['Desktop Chrome'] },
  webServer: {
    command: `..\\.venv\\Scripts\\python.exe -m agentguard.api.http --demo --port 8001 --database .agentguard/e2e-${Date.now()}.db --frontend dist`,
    url: 'http://127.0.0.1:8001/api/v1/health', reuseExistingServer: false, timeout: 30000,
  },
})
