# AI Market Watcher 本地发布候选检查报告

> 状态：本地发布候选，未创建 GitHub 仓库，未 push，未创建 Release。
>
> 检查日期：2026-08-19
>
> 参考方法：`/Users/huangruiheng/Downloads/封装参考/`

## 1. 本次结果

已用显式文件白名单生成独立发布包：

```text
packages/ai-market-watcher-v0.1.0/
packages/ai-market-watcher-v0.1.0.zip
packages/SHA256SUMS.txt
```

ZIP 大小约 120 KB，发布目录包含 28 个文件。

## 2. 已验证

```text
公开源文件白名单复制                         通过
排除 .env / cache / output / SQLite             通过
排除 roadshow 内部交接计划                     通过
排除个人绝对路径和凭据                         通过
ZIP 内容检查                                   通过
发布包内单元测试                               47/47 通过
发布包内离线 demo                              通过
SHA-256 清单生成                               通过
```

验证命令：

```bash
python scripts/build_release.py
python tests/validate_release.py
```

发布包内验证：

```bash
cd packages/ai-market-watcher-v0.1.0
python -m unittest discover -s tests -q
python run_demo.py
```

## 3. 封装边界

已放入公开包：

- 核心扫描器和状态链代码；
- `run_demo.py` 和 `run_live.py`；
- `README.md`、架构说明、数据源说明；
- `.env.example` 占位配置；
- 离线 demo 数据；
- 测试、构建脚本和发布检查脚本。

明确没有放入：

- 原始工作区；
- `.venv/`、`__pycache__/`；
- `cache/`、`output/`、SQLite 数据库；
- 真实 API Key、`.env`、凭据文件；
- Kimi HTML 路演计划和其他内部交接材料；
- GitHub remote、提交或发布状态。

## 4. 当前仍需处理

1. 公开项目尚未确定并加入 License；不能自行替用户选择许可证。
2. `v0.1.0` 目前只是本地候选版本号，不代表用户验收或正式发布。
3. 真实 Twelve Data 路演链路尚未在本次封装检查中运行，避免消耗 API 额度；只验证了入口、参数帮助和离线路径。
4. GitHub 创建、首次 push、Release 和下载链接核验仍需单独授权。

## 5. 结论

当前公开版已经具备“可复现构建 + 独立 ZIP + 安全检查 + 离线运行验证”的第一版封装骨架。

建议状态：

```text
本地发布候选：通过自检
正式公开发布：等待 License、用户确认和单独发布授权
```
