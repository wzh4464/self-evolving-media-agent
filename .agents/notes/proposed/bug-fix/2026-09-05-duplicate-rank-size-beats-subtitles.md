# 重复集取舍：体积当兜底，把生肉判赢了带字幕的

**日期**: 2026-09-05
**状态**: proposed / bug-fix
**触发**: 尼古喵喵 S01E08 是 ABEMA 生肉，其余 8 集是 LoliHouse 邪竜解放版

## 经过

2026-08-21，同一集出现两个候选：

    07:54  [Dynamis One] Yani Neko - 08 (ABEMA 1920x1080 AVC AAC MKV)   710MB  先到，已改名占位
    18:22  [LoliHouse]   Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC SRTx2]  566MB  后到

审计里连着两条：

    18:22:09 unrenamed-file    rename skipped   LoliHouse 那个 → 尼古喵喵 S01E08.mkv（集位被占）
    18:22:10 duplicate-episode trash   applied  保留 尼古喵喵 S01E08.mkv，清理 LoliHouse 那个

结果：库里留下一个**一条字幕轨都没有**的生肉，AutoBangumi 按规则正确下载的
LoliHouse 版被删了。AB 自己的记录（torrent id=804, downloaded=1）显示它确实下过。

## 根因

`DuplicateEpisode` 的排序键：

    parse_quality(f.torrent_name or f.filename, f.size).rank()
    # rank() = (height, simplified, is_bdrip, size)

实测两个候选：

    Dynamis One : (1080, False, False, 745065995)   ← 赢
    LoliHouse   : (1080, False, False, 593601176)

**两个问题叠加：**

1. **`simplified` 读错了字符串。** LoliHouse 单文件种子的 `torrent_name`
   就是内部文件名 `[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv`，
   里面写的是 `SRTx2`，没有「简」字。「简繁内封字幕」只存在于 Mikan 的**展示标题**
   （AB 库里存着：`[LoliHouse] 尼古喵喵 (邪竜解放版) / ... [简繁内封字幕]`）。
   于是带双字幕的那个被判成 `简=False`，和生肉并列。

2. **并列后落到 `size`，而 size 跨编码不可比。** HEVC-10bit 566MB 的画质
   高于 AVC 710MB。体积只有在**同编码**时才是码率的合理代理。

## 最关键的一点

判断依据全程来自**文件名猜测**，而地面真相就在文件里：
LoliHouse 那个有 2 条 subrip 轨（chi 简体 / chi 繁體），Dynamis One 一条都没有。
`parse_quality` 从不打开文件。

## 建议

排序键改成（优先级从高到低）：

1. `height`
2. **实际字幕能力**：容器内字幕轨数 + 语言（含外挂 `.ass/.srt/.sup` 同名文件），
   简体 > 繁体 > 仅日文/无。用 ffprobe 读，不再猜文件名。
   —— 这一档直接决定本例，且对用户「优先简中」的偏好是硬要求。
3. `is_bdrip`
4. **同编码内**比 size；跨编码时先按编码效率归一
   （HEVC/AV1 ≈ AVC 的 0.6×），或干脆在编码不同时放弃用 size 判定、
   转而报 finding 让人来选。

另外 `parse_quality` 的输入应尽量取**站点展示标题**（AB 的 `torrent.name`
字段存的就是这个），而不是种子内部文件名——两者对字幕的描述经常不一致。

## 副作用警告

这个 bug 会**系统性偏向先到者**：先到的已被改名占位，后到的因「集位被占」
rename skipped，随后在 duplicate 判定里若 rank 并列或更低就被删。
也就是说「谁先出要谁」的抓取模型 + 体积兜底 = 后来的更好版本会被主动删掉。
