# 参与贡献

欢迎提 issue 和 pull request。

- 改动前先跑一遍三组测试（见 README 的「快速开始」），提交时保持全部通过。
- 内核只用 Python 标准库和 SQLite（测试另需 `requirements-dev.txt` 里的 jsonschema）。需要新增依赖时，请先开 issue 讨论。
- 测试只用合成数据和假模型，不联网、不调用付费接口。
- 不要提交任何密钥、token、账号、真实聊天记录或个人信息。CI 会用 gitleaks 扫描。
- 提交 pull request 即表示你同意按 Apache License 2.0 授权你的代码贡献；示例世界的内容按 CC BY 4.0 授权。

# Contributing

Issues and pull requests are welcome. Keep all three test suites green, use only the Python standard
library, keep tests synthetic and offline, and never commit secrets or personal data. Contributions are
licensed under Apache-2.0 (code) and CC BY 4.0 (example world content).
