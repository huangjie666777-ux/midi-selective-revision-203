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

### POST /revise

在 `/compare` 的字段之外，再上传一个 JSON 文件字段 `actions`
（1–1024 条动作，≤ 2 MiB，作为文件部分上传以避免表单字段大小限制）：

```bash
curl -s -OJ -F reference=@examples/reference.mid -F performance=@examples/performance.mid \
     -F ref_track=1 -F ref_channel=0 -F perf_track=1 -F perf_channel=0 \
     -F actions=@examples/actions.json \
     http://127.0.0.1:8000/revise
```

动作依据**原对齐**的错误类别与音索引定位，不随动作顺序重新对齐：

| `type` | `index` 含义 | 效果 |
| --- | --- | --- |
| `fix_pitch` | 错音的实奏音索引 | 改实奏开/关音的音高为参考音高，保留起止 tick 与力度 |
| `delete_extra` | 多奏的实奏音索引 | 删除该音的开/关事件 |
| `add_missed` | 漏奏的参考音索引 | 按参考音高与起止绝对时间，经实奏 tempo 分段精确反解 tick（四舍五入、半 tick 向上）插入新音，开音力度 80，以 tick 0 计时不平移 |

统一规划全部动作：动作不存在、重复或矛盾，取整后零时长、负时间，
或最终所选通道音符重叠，均整次拒绝（422，`detail.file` 为
`"actions"`，`detail.event` 为动作序号），不交付任何文件。

成功返回 `application/zip`，含：

- `revised.mid`：修订后的实奏 SMF。保留 Type、PPQN、全部轨道、tempo
  及未修改事件的内容、绝对 tick 与相对顺序；同 tick 新增关音先于
  开音，必要时顺延轨尾；delta 重建为非负。
- `audit.json`：审计，记录两个源文件与修订文件的 SHA256、对齐摘要、
  每条动作的前后音高与 tick。

```bash
# 冲突示例：同一实奏音既改又删，整次拒绝
curl -s -F reference=@examples/reference.mid -F performance=@examples/performance.mid \
     -F ref_track=1 -F ref_channel=0 -F perf_track=1 -F perf_channel=0 \
     -F actions=@examples/actions_conflict.json \
     http://127.0.0.1:8000/revise
```

## 规则与边界

- 文件：仅 SMF Type 0/1 且正 PPQN；拒绝 Type 2 与 SMPTE 时间格式。
  每文件 ≤ 2 MiB、≤ 32 轨、≤ 40000 事件；旋律 1–1024 音。
- 音符：累积 delta tick；选定通道正力度 `note_on` 开音，`note_off` 与
  零力度 `note_on` 关音；同 tick 先关再开。拒绝孤立关音、未闭合音、
  零时长音与任意重叠音（含异音高重叠）。其他通道与踏板等事件忽略。
- 时间：Type 1 从轨 0 取 tempo，Type 0 取唯一轨；缺省 500000 微秒/四分
  音符。拒绝零 tempo 与同 tick 重复 tempo。按速度段以有理数
  （Fraction）累加微秒再换算毫秒。
- 对齐：DP 全局顺序对齐。同音高配对代价 0、异音高配对 2、漏奏与多奏
  各 1；回溯同成本时优先配对，其次漏奏，最后多奏。每个音在配对、
  漏奏、多奏中恰出现一次。

## 结构

- `midi_revision203/midi_loader.py`：SMF 解析、限量校验、tempo 时间线、旋律提取。
- `midi_revision203/align.py`：动态规划对齐与错误分类。
- `midi_revision203/revision.py`：动作解析、统一修订规划与 SMF 重写。
- `midi_revision203/main.py`：FastAPI 路由与 422 定位响应。
- `midi_revision203/errors.py`：携带文件/轨道/事件位置的拒绝异常。
- `examples/make_examples.py`：生成 `reference.mid`、`performance.mid`
  及两个反例文件；`actions.json` / `actions_conflict.json` 为修订动作示例。
- `tests/test_compare.py` / `tests/test_revise.py`：覆盖对齐、时间换算、修订规划与各类拒绝。

当前项目仓库：https://github.com/huangjie666777-ux/midi-selective-revision-203
