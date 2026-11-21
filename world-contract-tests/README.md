# world-contract-tests

世界内核的契约测试：版本化的合成场景（`cases.json`，由 `build_cases.py` 生成）和判定器 `oracle.py`。
判定器只读适配器导出的证据（traces），不会执行适配器，也不会发起任何真实调用。

```sh
python3 build_cases.py
PYTHONPATH=../world-catalog python3 -m unittest discover -s . -p 'test_*.py'
python3 oracle.py                                         # 只检查夹具：退出码 2，状态 CORPUS_VALID_SUT_NOT_RUN
python3 oracle.py --traces ./traces --report ./verdict.json
```

只有提供了完整证据且全部满足时才退出 0。任一缺文件、漏字段、身份错误、秘密泄漏、库存负数、重复效果或断言失败都退出 1。
