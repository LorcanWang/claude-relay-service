手动触发指定服务器的对话日志同步到 Firebase Firestore。

## 服务器列表

| 名称 | IP | 用户 | 密码 | 同步方式 |
|------|-----|------|------|----------|
| US-VPS | 65.49.213.189 | root | sHv5GMmj1wHf | nvm + node 直接执行 |
| HQ-Docker | 10.118.18.188 | hqzn | 123456 | docker exec |
| US-Node | 104.194.95.137 | root | r7DLHPGmBFw0 | node 直接执行 |
| HQ-Mac | 10.118.18.39 | test | 2e0ns0luti0ns | node 直接执行 |

## 参数

$ARGUMENTS — 可选，指定服务器名称（如 `US-VPS`、`HQ-Docker`、`all`）。不传则列出所有服务器让用户选择。

## 同步命令

### PM2 / 直接 node 部署（65.49.213.189 / 104.194.95.137 / 10.118.18.39）

```bash
node -e "
const redis = require('./src/models/redis');
const syncService = require('./src/services/conversationLogSyncService');
(async () => {
  await redis.connect();
  await syncService.syncToFirestore();
  await redis.disconnect();
  console.log('done');
  process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
"
```

### Docker 部署（10.118.18.188）

```bash
docker exec claude-relay-service-claude-relay-1 node -e "上述同样的脚本"
```

## 注意事项

- 65.49.213.189 和 104.194.95.137 需要通过 nvm 加载 node：`export NVM_DIR="$HOME/.nvm" && source "$NVM_DIR/nvm.sh"`
- 10.118.18.39 是 macOS，node 路径：`export PATH="/usr/local/opt/node@22/bin:$PATH"`
- 项目目录：65.49.213.189 和 104.194.95.137 在 `/root/claude-relay-service/app`，其余在 `~/claude-relay-service`
- 前置条件：`config/firebase-service-account.json` 必须存在
- 同步只读取 Redis 数据，不会删除或修改
