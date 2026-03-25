将本地 main 分支的最新代码部署到指定服务器。不会触碰 Redis 数据。

## 服务器列表

| 名称 | IP | 用户 | 密码 | 部署方式 |
|------|-----|------|------|----------|
| US-VPS | 65.49.213.189 | root | sHv5GMmj1wHf | PM2 (`claude-relay`) |
| HQ-Docker | 10.118.18.188 | hqzn | 123456 | Docker (`docker compose`) |
| US-Node | 104.194.95.137 | root | r7DLHPGmBFw0 | 直接 node 进程 |
| HQ-Mac | 10.118.18.39 | test | 2e0ns0luti0ns | 直接 node 进程 (macOS) |

## 参数

$ARGUMENTS — 可选，指定服务器名称（如 `US-VPS`、`HQ-Docker`、`all`）。不传则列出所有服务器让用户选择。

## 部署步骤

对每台目标服务器执行：

1. SSH 连接：`sshpass -p '<密码>' ssh -o StrictHostKeyChecking=no -o PreferredAuthentications=password <用户>@<IP>`
2. 进入项目目录：
   - 65.49.213.189 → `/root/claude-relay-service/app`
   - 10.118.18.188 → `~/claude-relay-service`
   - 104.194.95.137 → `/root/claude-relay-service/app`
   - 10.118.18.39 → `~/claude-relay-service`
3. 拉取代码：`git pull origin main`
4. 安装依赖（如果 package.json 有变更）
5. 重启服务（按部署方式）：
   - **PM2**: `pm2 restart claude-relay`
   - **Docker**: `docker compose up -d --build --no-deps claude-relay`（只重建 relay 容器，不碰 Redis）
   - **直接 node (Linux)**: `kill <pid> && setsid node src/app.js < /dev/null > /dev/null 2>&1 &`
   - **直接 node (macOS)**: `kill <pid> && nohup node src/app.js > /dev/null 2>&1 &`
6. 验证服务状态

## 注意事项

- 65.49.213.189 和 104.194.95.137 需要通过 nvm 加载 node：`export NVM_DIR="$HOME/.nvm" && source "$NVM_DIR/nvm.sh"`
- 10.118.18.39 是 macOS，node 路径：`/usr/local/opt/node@22/bin`，没有 setsid 命令，用 nohup
- 10.118.18.188 是 Docker 部署，npm install 在 docker build 阶段完成
- **绝不能** 重启 Redis 容器或清除 Redis 数据
- 部署完成后汇报每台服务器的状态
