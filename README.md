# pixiv-setu · 按你自己的偏好抽图

> 一个「有审美」的随机图机器人后端：**离线建池 + 在线打分 + 分级兜底**，出图不重复、不出冷门、AI 有配额。

不是「随机拉一张 pixiv 图」，而是：

1. **离线建池**：按你自己的偏好库（画师 + tag + 口语模糊词）用 pixiv App API 拉一遍作品，每条自带收藏数 / 浏览量，落成 `pool.json`；
2. **在线打分**：请求时给候选打三个分（偏好 tag 命中 / 质量分 / 画师命中），只在**总分前 30%**里随机；
3. **分级交付**：取图走 `pixiv.re` 镜像，图被平台吞掉时自动重编码 → 合并转发 → 转群文件，四级兜底。

---

## 特性

- **偏好可控**：分数来自你自己的表（82 位画师 / 163 个 tag / 271 个口语词都能自己填），不用猜模型喜好
- **不出低质量图**：收藏/浏览量双门槛 + 绝对质量分，冷门作品进不来（写过一篇复盘：bm 75 的图怎么混进前 12%）
- **不出重复图**：按群记录 7 天内发过的作品，出图前排除
- **AI 限流**：AI 作品总量 ≤30%，且必须质量分高于人工图才放行（令牌桶 + 多级窗口）
- **定向可控**：`来点黑丝色图` = 只在「黑丝」命中的 tag 子集里抽；`来点二次元` = 非 R18
- **指定画师**：`来点<画师名>`，支持模糊（`来点鬼针` → 鬼针草），取该画师作品质量前 80%
- **不需要常驻代理**：取图走 `pixiv.re`；代理只在「建池」那一刻用一下
- **抗 429**：App API 一条记录自带收藏数，避免「一张一详情」的请求风暴

---

## 它怎么工作

```
偏好表(xlsx)  ──pixdb.py──▶  db.json           画师 / tag / 模糊词索引
                                │
                     build_pool.py（App API，需要 refresh_token）
                                │
                             pool.json           每条：pid/tags/bm/view/AI/R18
                                │
   命令 ──▶ nonebot 插件 ──▶ pixcli.py pick ──▶ 打分选图 ──▶ pixiv.re 取图 ──▶ 压缩 ──▶ 发送
```

---

## 目录

```
.
├─ pixcli.py              # 出图 CLI（pick / status / tags / artists / build）
├─ build_pool.py          # 建池（App API，输出 pool.json）
├─ pixdb.py               # 偏好表(xlsx) -> db.json
├─ mkproxy.py             # 可选：订阅 -> xray 配置（仅建池时开个 socks）
├─ db/
│  └─ preferences_template.xlsx   # 偏好库模板（复制改名后填自己的）
├─ nonebot_plugin/
│  └─ pixiv.py           # nonebot2 插件（命令 + 7 天去重 + 吞图补发）
├─ docs/
│  ├─ 01-打分规则.md
│  ├─ 02-获取-refresh-token.md
│  └─ 03-常见问题.md
├─ env.example
└─ requirements.txt
```

---

## 快速开始（先把 CLI 跑通）

### 0. 准备

- Python **3.10+**（用到 `str | None` 注解）
- 一个 pixiv 账号的 **refresh_token**（见 `docs/02-获取-refresh-token.md`）
- 能访问 pixiv 的网络：直连不行就自备 socks5（`PIX_SOCKS`，默认 `127.0.0.1:10808`）

### 1. 装依赖

```bash
pip install -r requirements.txt
```

### 2. 放偏好表

把 `db/preferences_template.xlsx` 复制成你自己的表（可以放多份，读取时会合并去重）：

```
db/
├─ preferences_template.xlsx   # 模板，留着当参考
└─ my_taste.xlsx               # 你自己的
```

必须保留的列：画师表的 **用户ID**、Tag 表的 **日文Tag**、模糊词表的 **模糊词（用户会说的）**。
按列名读取，列顺序随意；缺列会在 `pixdb.py` 的 `problems` 里报出来。

### 3. 生成索引

```bash
python pixdb.py            # 读 db/*.xlsx -> db.json
```

### 4. 配 token

```bash
cp env.example env
vi env                      # 填 PIX_REFRESH_TOKEN
chmod 600 env
```

`env` 和 `db.json`、`pool.json` 都在 `.gitignore` 里，**不会被提交**。

### 5. 建池

```bash
python build_pool.py        # 163 tag × 2 模式 + 82 画师，约 20~25 分钟
python pixcli.py status     # 看池子规模
```

### 6. 出图

```bash
python pixcli.py pick --mode anime --out /tmp/a.jpg     # 非 R18
python pixcli.py pick --mode lewd  --out /tmp/b.jpg     # R18
python pixcli.py pick --mode lewd --fuzzy 黑丝 --out /tmp/c.jpg
python pixcli.py pick --artist 鬼针 --out /tmp/d.jpg    # 指定画师（模糊）
python pixcli.py tags   --keyword 黒                    # tag 列表
python pixcli.py artists --keyword 鬼                   # 画师列表
```

输出是一行 JSON：`{ok, pid, author, title, tags, bm, view, score, s1, s2, s3, hit_tags, cand, top, file, ...}`。

---

## 打分规则（这是本项目的核心）

候选集先按 **R18 / 口语词** 过滤，再剔除 **7 天内发过的**，然后打分：

| 分项 | 含义 | 默认权重 |
|---|---|---|
| `s1` | 命中偏好 tag（取前 3 个，超出不再加分） | 0.6 |
| `s2` | 质量分：`0.5×全池分位^0.75 + 0.5×绝对质量` | 1.0 |
| `s3` | 命中偏好画师 | 0.3 |

- 绝对质量：`0.75×log10(bm+1)/log10(3001) + 0.25×log10(view+1)/log10(30001)`（收藏权重高于浏览）
- 硬门槛：非 R18 需 `bm≥400 或 view≥8000`；R18 需 `bm≥150 或 view≥2000`
- 只在**总分前 30%** 里按 `score²` 加权随机 —— 不是取第一名，所以不会天天同一张
- AI 单独走令牌桶：总量 ≤30%，且 `s2` 要高于人工图中位数 ×1.25 才放行

细节和踩坑复盘见 `docs/01-打分规则.md`。

---

## 装进 nonebot2

```bash
cp nonebot_plugin/pixiv.py <你的bot>/src/plugins/
```

插件读环境变量 `PIX_ROOT` 找 CLI（默认 `/opt/pixiv-setu`），并把图写到 `MATERIAL_ROOT/随机色图/`
（NapCat 在容器里跑时，这个目录必须是挂载进去的那个，否则会 `ENOENT`）。

命令：

| 命令 | 说明 |
|---|---|
| `来点二次元` / `来点色图` | 非 R18 / R18 |
| `来点黑丝色图`、`来点女仆装二次元` | 口语词 + 类别，只在命中的 tag 子集里抽 |
| `来点<画师名>` | 指定画师（支持模糊） |
| `画师列表` / `tag列表` | 看池子里有什么，可带关键词筛选 |
| `色图状态` / `重建色图池` | 管理员 |

配套行为：

- **7 天去重**：`pix_sent(group_id, pid, ts)` 记录，发送前查、发送后写
- **图被吞**：先发图再发文字；用户回「图被吞了」时走 ①合并转发 → ②重编码直发 → ③传群文件 → ④明确报失败
- **定时重建**：给 `build_pool.py` 配一个 systemd timer（示例注解见 `nonebot_plugin/pixiv.py` 顶部）

---

## 配置项

全部走环境变量（或 `env` 文件）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PIX_REFRESH_TOKEN` | — | **必填**，pixiv App refresh_token |
| `PIX_ROOT` | 脚本目录 | 池子 / 缓存 / env 的根 |
| `PIX_SOCKS` | `127.0.0.1:10808` | 建池用的 socks5（取图不需要） |
| `PIX_TAG_PAGES` | 1 | 每个 tag 抓几页（每页 30 条） |
| `PIX_ART_PAGES` | 2 | 每位画师抓几页 |
| `PIX_API_SLEEP` | 0.9 | 请求间隔；被 429 就调到 2.0 |
| `PIX_BM_W` / `PIX_VIEW_W` | 0.75 / 0.25 | 质量分权重 |
| `PIX_TOP_RATIO` | 0.30 | 只在总分前 30% 里抽 |
| `PIX_AI_RATIO` | 0.30 | AI 上限 |
| `PIX_MIN_BM_ABS` / `PIX_MIN_VIEW_ABS` | 400 / 8000 | 非 R18 门槛 |
| `PIX_MIN_BM_ABS_R18` / `PIX_MIN_VIEW_ABS_R18` | 150 / 2000 | R18 门槛 |
| `PIX_MAX_SIDE` / `PIX_MAX_BYTES` | 1600 / 1500000 | 出图压缩 |

---

## 合规与免责

- 本项目只包含**代码**，不附带任何图片、账号、cookie 或 token；`pool.json` / `db.json` / `env` 都在 `.gitignore` 里
- 所有请求都打到 pixiv 官方 App API 与 `pixiv.re` 镜像，请自行控制频率，**不要拿去刷量**
- R18 内容仅限你有权访问的场合（私人群、自己服务器），群功能默认应关闭 R18
- 公开分发他人作品可能侵权：本项目只做「按需取图 + 即时发送」，不做二次分发、不建镜像站
- 使用即表示你自行承担账号风控、内容合规等一切后果

MIT License.
