# MIDI演奏修订后端203

上传参考与实奏两个 Standard MIDI File，提取指定轨道/通道的单旋律，
按各自速度图换算毫秒时间轴，再用动态规划做全局顺序对齐，返回总成本、
正确数与错音/漏奏/多奏三类错误明细，以及每对音符的起音与时长偏差。

## 运行

```bash
.venv/bin/python examples/make_examples.py   # 生成示例 MIDI
.venv/bin/python -m uvicorn midi_revision203.main:app --port 8000
```

测试与编译检查：

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/python -m compileall -q midi_revision203 tests examples
```

## 接口

### POST /compare

multipart/form-data：

| 字段 | 说明 |
| --- | --- |
| `reference` | 参考 SMF 文件 |
| `performance` | 实奏 SMF 文件 |
| `ref_track` / `perf_track` | 0 起轨号 |
| `ref_channel` / `perf_channel` | 通道 0–15 |

```bash
curl -s -F reference=@examples/reference.mid -F performance=@examples/performance.mid \
     -F ref_track=1 -F ref_channel=0 -F perf_track=1 -F perf_channel=0 \
     http://127.0.0.1:8000/compare
```

响应（200）要点：

- `total_cost`：对齐总成本；`correct_count`：同音高配对数。
- `pairs[]`：每个配对含 `ref_index`/`perf_index`、双方音高、
  `onset_ms_deviation` 与 `duration_ms_deviation`（实奏减参考，毫秒，
  均以各自文件 tick 0 为起点，不做自动平移）。
- `errors.wrong_pitch / missed / extra`：三类错误的 `count` 与 `items` 明细。
- `reference.notes[]` / `performance.notes[]`：每音的原事件序号
  （`event_index`，轨道内 0 起）、音高、起止 tick 与起止毫秒。

非法材料整次拒绝，返回 422，`detail` 定位 `file`
（reference/performance）、`track` 与 `event`：

```bash
curl -s -F reference=@examples/bad_overlap.mid -F performance=@examples/performance.mid \
     -F ref_track=0 -F ref_channel=0 -F perf_track=1 -F perf_channel=0 \
     http://127.0.0.1:8000/compare
# {"detail":{"message":"overlapping note for pitch 60","file":"reference","track":0,"event":1}}
```

请求之间相互独立，不保存任何状态。

## 规则与边界

- 文件：仅 SMF Type 0/1 且正 PPQN；拒绝 Type 2 与 SMPTE 时间格式。
  每文件 ≤ 2 MiB、≤ 32 轨、≤ 40000 事件；旋律 1–1024 音。
- 音符：累积 delta tick；选定通道正力度 `note_on` 开音，`note_off` 与
  零力度 `note_on` 关音；同 tick 先关再开。拒绝孤立关音、未闭合音、
  零时长音与同音高重叠音。其他通道与踏板等事件忽略。
- 时间：Type 1 从轨 0 取 tempo，Type 0 取唯一轨；缺省 500000 微秒/四分
  音符。拒绝零 tempo 与同 tick 重复 tempo。按速度段以有理数
  （Fraction）累加微秒再换算毫秒。
- 对齐：DP 全局顺序对齐。同音高配对代价 0、异音高配对 2、漏奏与多奏
  各 1；回溯同成本时优先配对，其次漏奏，最后多奏。每个音在配对、
  漏奏、多奏中恰出现一次。

## 结构

- `midi_revision203/midi_loader.py`：SMF 解析、限量校验、tempo 时间线、旋律提取。
- `midi_revision203/align.py`：动态规划对齐与错误分类。
- `midi_revision203/main.py`：FastAPI 路由与 422 定位响应。
- `midi_revision203/errors.py`：携带文件/轨道/事件位置的拒绝异常。
- `examples/make_examples.py`：生成 `reference.mid`、`performance.mid`
  及两个反例文件。
- `tests/test_compare.py`：20 个用例覆盖对齐、时间换算与各类拒绝。

当前项目仓库：https://github.com/huangjie666777-ux/midi-selective-revision-203
