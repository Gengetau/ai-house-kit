# world-catalog

活动目录与统一词表。规范数据是 `worldcatalog/catalog.json`；示例世界在 `worldcatalog/life1/`。

内核在命令接收、动作开始和完成前都要调用 `guard_request`，使用权威的角色和资源行；返回非空理由就拒绝，不提交效果。
不能用模型报上来的库存冒充权威资源。

```sh
PYTHONPATH=. python3 -m worldcatalog                                   # 打印目录摘要
PYTHONPATH=. python3 -m worldcatalog --projection world-vocab.v1.json --check   # 检查投影是否最新
PYTHONPATH=. python3 -m unittest discover -s . -p 'test_*.py'
python3 check_kernel_compat.py --kernel ../world-kernel                # 用目录替换后的词表跑内核测试
```

修改规范 JSON 后，去掉 `--check` 重新生成投影再提交。
