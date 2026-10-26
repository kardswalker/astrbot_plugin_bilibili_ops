# astrbot_plugin_bilibili_ops

让 AstrBot 里的 LLM 使用一个真实 B 站账号：

- **只读**：查视频信息、读视频评论、读指定动态
- **发布**：发评论、发动态 —— 必须经过 **白名单 → 人工确认 → 限频** 三道保护

## 安装

AstrBot WebUI → 插件 → 从仓库/文件夹安装；依赖（`bilibili-api-python`、`aiohttp`）会自动安装。
国内服务器若安装慢，请先给容器配置 pip 镜像。

## 获取 Cookie

1. 用目标账号登录 <https://www.bilibili.com>。
2. 浏览器开发者工具 → 应用（Application）→ Cookie → `https://www.bilibili.com`。
3. 复制 `SESSDATA`、`bili_jct`、`buvid3`、`DedeUserID` 的值，填入插件配置。

Cookie 等同于账号登录态，**不要**发给任何人、不要提交到仓库。Cookie 会过期，用 `/bili 状态` 检查。

## 配置

| 项 | 说明 | 默认 |
|---|---|---|
| sessdata / bili_jct / buvid3 / dedeuserid | B 站 Cookie | 空 |
| allowed_qq_ids | 可发布的 QQ 号白名单，**空 = 禁止一切发布** | 空 |
| confirm_ttl_seconds | 确认有效期 | 120 |
| max_posts_per_hour | 每小时最多发布次数 | 5 |
| min_interval_seconds | 两次发布最小间隔 | 30 |
| max_text_length | 评论/动态最大字数 | 200 |
| read_only_mode | 只读模式，拒绝所有发布 | false |

> 配置存放在 `data/config/` 下的 JSON 中。Cookie 项在 schema 中标记了 `secret: true`，
> 但我**未能确认**所用 AstrBot 版本的 WebUI 是否会渲染为密码框，请安装后核对；
> 无论如何，请保护好 `data/` 目录权限。

## 使用

LLM 工具：`bili_video_info`、`bili_get_comments`、`bili_get_dynamic`、`bili_post_comment`、`bili_post_dynamic`。

发布流程：

1. LLM 调用发布工具 → 校验白名单、只读、字数、限频 → **只生成待确认请求并返回预览，不向 B 站写任何东西**。
2. 发起人发送 `/bili 确认 <确认码>`（指令，不经过 LLM）。会再次校验白名单、有效期、是否同一发起人、限频，然后才真正发送。
3. `/bili 取消` 放弃；超时自动作废。

其他：`/bili 状态` 检查 Cookie 是否有效。

没有“免确认”开关：任何发布都必须由白名单用户本人确认。

## 风险提示

- 自动化发布可能触发 B 站风控甚至封号。默认限频较保守，请勿调高后批量使用。建议先用小号测试。
- 评论、简介等外部内容可能夹带提示词注入。插件把它们标记为“不可信数据”，且发布只能由白名单用户的确认指令触发；确认前请仔细阅读预览内容。
- 发布成功/失败的日志只记录动作和 BV 号，不记录 Cookie 与正文。
- 限频记录保存在插件数据目录，重启不会清零。

## 开发与测试

```bash
pip install pytest bilibili-api-python aiohttp
python -m pytest tests
```

核心逻辑在 `core.py`（不依赖 AstrBot），`bili.py` 封装 B 站接口，`main.py` 是 AstrBot 胶水层。
