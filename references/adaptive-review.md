# 自适应视听审查协议

本协议与具体 AI 平台无关。脚本只负责时间对齐、计划校验和增量抽帧；画面语义判断由人或任意具备读图能力的模型完成。

## 一、处理闭环

1. `watch.py` 产出首轮 `transcript.json`、`frames/frames.json` 和 `review.json`。
2. 审查者读取 `review.json` 中每个时间窗列出的文本与图片，填写 `assessment`。
3. `review.py plan` 把需要补证据的窗口转换成 `refine_plan.json`。
   若结果中的 `intervals` 为 0，直接停止，不要把空计划传给 `refine.py`。
4. `refine.py` 校验计划并调用 `frames.py --append`，保留首轮帧并沿用已有分辨率。
5. 运行 `review.py refresh --review <run_dir>/review.json`；它只重置新增帧影响到的窗口。
6. 只复查被重置的窗口。默认最多两轮、累计最多 120 张补帧；证据已经足够或预算耗尽时停止。

## 二、关系标签

- `supports`：画面直接支持文稿中的陈述。
- `complements`：画面提供不同但相关的信息，例如讲话解释图表。不要因为文本和画面字面不同而补帧。
- `contradicts`：画面与文稿明确相反。先在前后小范围补帧，排除转场、字幕漂移或音画偏移。
- `unrelated`：人物镜头、装饰性 B-roll 等合理无关画面。
- `insufficient`：没有覆盖关键时刻、帧模糊/黑屏、正处于转场，或现有证据不足以判断。

每个窗口的判断格式：

```json
{
  "assessment": {
    "relation": "insufficient",
    "confidence": 0.86,
    "notes": "讲话要求查看右侧曲线，但最近帧距该时刻 4.2 秒",
    "refine": true
  }
}
```

只有 `insufficient` 应直接触发补帧。`contradicts` 只用于短窗口诊断，不能无限加帧。

## 三、补帧计划

计划支持时间区间和精确时间点：

```json
{
  "version": 1,
  "intervals": [
    {
      "start": 120.0,
      "end": 126.0,
      "reason": "insufficient_visual_evidence",
      "fps": 2
    }
  ],
  "times": [
    {
      "t": 242.5,
      "reason": "diagnose_possible_contradiction",
      "source": "explicit"
    }
  ]
}
```

约束：

- `start`、`end`、`t` 使用源视频绝对秒数。
- 手写计划的 `fps` 省略时按 2 处理；`review.py plan` 为快速操作默认生成 4fps 计划。局部最高 4fps，基础抽帧仍保持 2fps 上限。
- `refine.py` 默认每轮最多新增 60 帧、累计最多 120 帧，并强制最多两个不同的补帧 pass；重复或过近的请求时间点自动去重。
- 使用唯一 `pass_id`，例如 `r1`、`r2`。同一标识重跑时 manifest 记录会更新。
- 不要把整片视频塞入一个高帧率区间；优先选择未覆盖场景点、视觉提示语附近和相邻帧中点。

## 四、停止条件

满足任一条件即停止：

- 对应窗口已经变为 `supports`、`complements` 或可解释的 `unrelated`。
- `contradicts` 在前后多帧中持续存在，应报告真实冲突，而不是继续补帧。
- 已完成两轮。
- 总新增帧达到预算。
- 本轮 `added_count` 为 0，说明计划时间点已被现有证据覆盖。
- 时间间隔已缩小到 0.25 秒，继续加帧没有明显新信息。

烧录字幕可能让 OCR 与文稿高度相似，却不能证明主体画面已经被理解；静默演示又没有文稿可比较。因此应同时关注时间覆盖、场景变化、帧质量和无语音画面活动。

没有文稿时 `watch.py` 仍会生成视觉覆盖审查包，但任何离散抽帧方案都无法保证发现恰好落在两个采样点之间的短暂静默动作。对这类高风险视频，应主动缩小关注区间、使用 4fps 定向抽帧，或直接逐段播放核验。
