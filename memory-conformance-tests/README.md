# memory-conformance-tests

私有部署自己的记忆测试，原样搬来，对着 kit 里的公共记忆模块（`src/ai_house/common/memory.py`）跑。
它们通过，说明这份公共代码的行为与私有部署一致；它们不测 kit 的 `MemoryStore`。

```bash
cd memory-conformance-tests
PYTHONPATH=../src python3 -m unittest discover -s . -p 'test_*.py'
```

- `test_memory_*.py`：9 个文件、113 个用例，来源与改动见 `src/ai_house/common/UPSTREAM.md`。
- `roster.py`：测试用的花名册，人物和号码都是编的。
- `memory.py`：让测试里的 `import memory` 拿到公共模块的一份副本。

不要为了让它们通过而改这里的测试；要改，先确认私有端的同名测试也该这么改。
