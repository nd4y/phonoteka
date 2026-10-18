# Статус реализации

Последний подтвержденный production-выпуск - **0.6.0-alpha**. [Описание действующего выпуска](current-release.md) не изменено. Кандидат0.7, HEAD `801981f75276c60661cc3b823e41e91481e83ab6`, прошел полный gate build-01, но еще не принят целиком и не развернут на st1. GitHub dispatch39160004444 на момент среза выполняется; настоящая ASR-ветка прошла, весь workflow заранее зеленым не считается.

[Acceptance-audit](acceptance-audit.md) содержит все84AC и CHG-001..032, точные артефакты и конечные этапы S1..S7. В этом документе наличие кода отделено от приемки. Неизвестная запись, provider no_match или поврежденный файл остаются явным результатом, а не принудительным success.

## Реализовано в коде кандидата

| Область | Рабочий контракт | Еще не подтверждено |
| --- | --- | --- |
| Доступ | Fixed single owner, Argon2, sessions/CSRF/origin, scoped expiring tokens, logout-all/local reset и effect authorization | Новая st1 HTTPS/bootstrap/revocation приемка; OIDC выключен. |
| Каталог/CUE | Separate work/RO original, content signatures, Unicode search, fullSHA/hash_verified_at, invalid/RO profiles; CUE encodings/INDEX, PCM-verified derivatives | Свежий полный аудит реальной коллекции/источника и новый browser CUE navigation. |
| Очереди | Bounded async/executor, independent ordinary/inference lanes, pause/cancel/resume/recovery; percent/counts/ETA/actions/SSE | Нормированный15/30-минутный профиль и browser states/reconnect. |
| Источники/YouTube | MB/LRCLIB/CAA/local/ShazamIO, consent/rights/TTL/rate/retry/deadline/cache/budget/UsageAttempt; YouTube evidence/search/owner link без media downloads | Актуальное покрытие и реальные варианты исполнения. Paid/keyed providers без настройки не подключены. |
| ASR/ALIGN | Отдельный pinned CPU image, durable dedup, bounded decode/PCM/temp/model/result process; independent ASR anchors+DTW/original timeline | Музыкальные30+10 случаев и долгий фактический ресурсный профиль. GPU/remote/vocal separation не включены. |
| Текст/предложения | Durable IDs/nullable times/metadata, independent words/timing revisions/locks, encoding preview/listening review; field locks/priorities/manual first/provenance, immutable proposals/CAS, named v1 routes и album batch | Browser приемка; точность слов/времен на музыке. Выбор кандидата/сохранение proposal не запускают запись автоматически. |
| Native media/artwork | MP3/FLAC/MP4/Vorbis/Opus/WAV/AIFF, explicit cover/lyrics slots, packet preservation/reopen/backup/journals; isolated JPEG/PNG20MiB/25MP decoder, configurable resize без crop/upscale, smaller-cover confirmation | Реальный корпус/gapless/телефон. Известные права сохраняются через resize и остальные слоты; профиль за пределами возможностей остается RO. |
| Audit/duplicates/albums/Gain | All-stream decode before recognition/Ready; silence/peak warnings, unknown lossless; exact/packet/candidate/A-B/preferred; exact MB release/subset; Track/joint Album Gain с frozen composition/audio/release CAS и lock до DB commit | Реальные mixed package и browser A-B; album-тег не доказывает комплектность, duplicates не удаляются. |
| Automatic gate | Единый versioned server gate/reason codes/fresh dependencies/auth/source/locks при enqueue и effect | **Default off, `PILOT_REGISTRY={}`, `pilot_not_validated`.** Unit-тест положительной ветки не заменяет независимый пилот. |
| Ready/export/delivery | Immutable plan/hash/expected Ready CAS/accepted profiles/independent rollback; opt-in sidecar owners/journals; immutable audio+cover+LRC manifest/M3U8/aliases; Range/leases/revision-bound receipts | Целевая поставка и Android/Poweramp. Partial не называется atomic batch; client success не доказывает hash/офлайн-подсветку. |
| Эксплуатация | Keep-all/pins/leases/CAS retention, waiting_storage; offline state backup/recovery; explicit checkpoint migration1->2 и legacy write barriers | Production migration/deploy/отдельный restore еще не выполнены. Live DB не подменяется старым snapshot. |
| Observability/UI/PWA | Dedicated metrics/JSON stdout/actions/Grafana47/optional outbox; Material3 themes/dock previous-next/shared seek/CUE/dirty guard/state persistence/animations/reduced motion/public-only SW | Реальный browser/PWA lifecycle и свежие scrape/logs/panels после поставки; webhook receiver не настроен. |

## Что действительно проверено

- [Чистый build-01 B79](../../output/music-library/release-07/a79ecdf-backend.xml), snapshot `a79ecdf2e950`, run `20261018T194149Z-195506`: **409 passed, 0 failed/error/skipped**, JUnit202.466с; frontend/runtime прошли. Новое isolated demo8177 запущено, foreign container IDs сохранены. Отдельный authenticated browser-review8178 создан на generated fixtures; сам запуск не доказывает действия в браузере.
- В B79 прошли реальные media53, native slots15, canonical-sidecars1, artwork7, fault14, migrations9, LyricsDocument6, group Gain7, API facades6, provider policy9, automatic gate23 и остальные модули. Это проверка текущего кода/tempFS, а не физической системы или музыкальных метрик.
- Прежние failed runs и diagnostic overrides сохранены как история, не как принятые образы. FFmpeg6-only note установлена отдельным official Ubuntu24.04 экспериментом; narrow classifier и fatal/unknown regressions прошли B79.
- Короткий PERF60 измерил10k записей/120HTTP/4 настоящих Worker, HTTP p95<1с и actual parallelism. SSE старого среза не прошел. >=15минут/1000HTTP и30минут RAM/fd/tasks/lag еще не выполнены.
- Настоящая ASR inference ветка CI проверяет синтезированную речь/align/wrong text/dedup. Это не закрытый музыкальный набор30+10 и не нынешний inference под длительной нагрузкой.

## Что осталось для завершения v1

Конечный список: S1 полный matched GitHub image gate; S2 browser/PWA/player/jobs/observability; S3 полный NFR/ASR resource profile; S4 controlled st1 deploy/migration/restart/restore/ACL/HTTPS; S5 свежий аудит и mixed package фактической коллекции; S6 независимый музыкальный пилот30+10; S7 actual one-way delivery20MP3/FLAC и offline Android/Poweramp. Точные пороги и соответствующие AC находятся в acceptance-audit.

Подтвержденных отсутствующих обязательных backend-маршрутов на текущем срезе не установлено. **Полное ТЗ не принято, уведомление «ТЗ выполнено» не отправляется.** Cloud/device file sources остаются только проектированием CHG-026. Optional OIDC/GPU/remote/keyed catalogs/webhook receiver не включаются ради отметки в таблице.

`requirements-register.md`, архивы ТЗ и `current-release.md` этим обновлением не изменялись. Production статус0.6 отделен от новой реализации и ее приемочных результатов.
