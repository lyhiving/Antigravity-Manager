# 🐋 Antigravity Manager 原生 Docker 部署手冊

本目錄包含 Antigravity Manager 的原生 Headless Docker 部署方案。該方案支持完整的 Web 管理界面、API 反代以及數據持久化，無需複雜的 VNC 或桌面環境。

## 🚀 快速開始

### 1. 直接拉取鏡像 (推薦)
您可以直接從 Docker Hub 拉取已構建好的鏡像並啟动，無需獲取源碼：

> [!IMPORTANT]
> **安全警告**：從 v4.0.2 開始，Docker 版支持 **管理密碼與 API Key 分離**：
> *   **API Key**：通過 `-e API_KEY=xxx` 設置，用於所有 AI 協議的 API 調用鑒權。
> *   **Web 管理密碼**：通過 `-e WEB_PASSWORD=xxx` 設置，僅用於 Web UI 登錄。
> *   **默認行為**：若未設置 `WEB_PASSWORD`，系統會自動回退使用 `API_KEY` 作為登錄密碼。若兩者皆未設置，則生成隨機 Key。
> *   **查看方式**：執行 `docker logs antigravity-manager` 尋找 `Current API Key` 或 `Web UI Password`，或執行 `grep -E '"api_key"|"admin_password"' ~/.antigravity_tools/gui_config.json` 查看。

```bash
# 啟動容器 (請替换 your-secret-key 為強密鑰)
docker run -d \
  --name antigravity-manager \
  -p 8045:8045 \
  -e API_KEY=your-api-key \
  -e WEB_PASSWORD=your-login-password \
  -v ~/.antigravity_tools:/root/.antigravity_tools \
  lbjlaq/antigravity-manager:latest
```

### 2. 使用 Docker Compose
在 `docker` 目錄下執行：
```bash
docker compose up -d
```

### 3. 手動構建鏡像 (開發者)
如果您需要修改代碼或自定義構建，請在項目根目錄下執行：
```bash
# 默認構建最新標籤
docker build -t antigravity-manager:latest -f docker/Dockerfile .
```

#### 💡 構建參數
本鏡像支持自動鏡像源切換，以提升国内構建速度：
*   `USE_MIRROR`: 
    *   `auto` (默認): 自動檢測網絡環境，若無法訪問 Google 則切換至国内镜像（阿里云/NPM Mirror）。
    *   `true`: 強制使用国内镜像源。
    *   `false`: 強制使用官方默認源。

示例：
```bash
# 強制使用国内镜像加速構建
docker build --build-arg USE_MIRROR=true -t antigravity-manager:latest -f docker/Dockerfile .
```

## ⚙️ 環境變量配置

| 變量名 | 默認值 | 說明 |
| :--- | :--- | :--- |
| `PORT` | `8045` | 容器內服務監聽端口 |
| `ABV_API_KEY` | - | **[重要]** 代理 API 密鑰。客戶端（如 Claude Code）訪問時需提供的 Key |
| `ABV_WEB_PASSWORD` | - | **[安全]** Web 管理後台登錄密碼。若不設置則回退使用 API Key |
| `LOG_LEVEL` | `info` | 日志等級 (debug, info, warn, error) |
| `ABV_DIST_PATH` | `/app/dist` | 前端靜態資源託管路徑 (Dockerfile 已內置) |
| `ABV_PUBLIC_URL` | - | 用於遠程 OAuth 回調的公網 URL (可選) |

## 📂 數據持久化
請務必將宿主機目錄掛載至容器內的 `/root/.antigravity_tools`，否則賬號和配置在容器重啟後會丟失。

## 🌐 訪問位址
*   **管理界面**: [http://localhost:8045](http://localhost:8045)
*   **API Base**: [http://localhost:8045/v1](http://localhost:8045/v1)

## 📦 Docker Hub 分發 (推薦)
若要推送至你的倉庫：
```bash
# 打上版本標籤並推送
docker tag antigravity-manager:latest lbjlaq/antigravity-manager:latest
docker tag antigravity-manager:latest lbjlaq/antigravity-manager:4.0.2
docker push lbjlaq/antigravity-manager:latest
docker push lbjlaq/antigravity-manager:4.0.2
```
