# 获取 pixiv refresh_token

本项目建池走 **App API**（`app-api.pixiv.net`），它需要 OAuth 的 `refresh_token`。

## ⚠ 先说结论：密码授权已经用不了

`pixivpy` 里的 `login(username, password)` 现在会返回：

```
{"has_error":true,"errors":{"system":{"message":"The grant type is unauthorized for this client_id","code":1508}},"error":"invalid_grant"}
```

pixiv 关掉了第三方 client 的 password 授权。能用的只剩两条：

1. **refresh_token**（本项目用这条）
2. 自己注册 OAuth client 走 authorization code（要开发者资质）

## 拿 refresh_token 的两条实用路线

### 路线 A：用现成工具登录一次（最省事）

在能开浏览器的机器上装 `gallery-dl` 或 `pixivpy` 的 OAuth 辅助脚本，登录一次拿到 token；或者用下面路线 B 手动走一遍，10 分钟。

### 路线 B：浏览器 + PKCE（手动，可复现）

1. 生成 PKCE 参数：

```python
import base64, hashlib, os
v = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('=')
c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip('=')
print('verifier:', v)
print('challenge:', c)
```

2. 浏览器打开（先登录 pixiv）：

```
https://app-api.pixiv.net/web/v1/login?code_challenge=<challenge>&code_challenge_method=S256&client=pixiv-android
```

3. 点「继续使用此账号」，在地址栏/网络面板里抓回调：

```
https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?state=...&code=<CODE>
```

4. 用 `code` 换 token（`client_id` / `client_secret` 是 pixiv 安卓客户端的公开常量，`pixivpy` 里就有）：

```bash
curl -s https://oauth.secure.pixiv.net/auth/token \
  -H 'User-Agent: PixivAndroidApp/6.0 (Android 12; Pixel)' \
  --data-urlencode 'client_id=<CLIENT_ID>' \
  --data-urlencode 'client_secret=<CLIENT_SECRET>' \
  --data-urlencode 'grant_type=authorization_code' \
  --data-urlencode 'include_policy=true' \
  --data-urlencode 'redirect_uri=https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback' \
  --data-urlencode 'code=<CODE>' \
  --data-urlencode 'code_verifier=<verifier>'
```

返回里的 `refresh_token` 就是项目要的，写进 `env`：

```
PIX_REFRESH_TOKEN=xxxxx
```

## token 会轮换吗

正常情况下 refresh 会**回同一个** refresh_token；如果哪天日志出现「refresh_token 发生轮换」，按提示把 `env` 改成新的即可（代码已打日志）。

## 拿到 token 能做什么

- ✅ 检索（含 R18）、画师作品列表、作品详情
- ✅ 每条记录自带 `total_bookmarks` / `total_view`，比网页端「一张一详情」省 30 倍请求
- ❌ 不要用它做刷量、批量下载、转存——风控会直接封号
