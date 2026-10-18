# Сверка ТЗ v1.4: реализация и приемка

Срез исходников: `801981f75276c60661cc3b823e41e91481e83ab6`, 18.10.2026. Основание - все 84 AC [исходного ТЗ](specification.md) и CHG-001..032 [реестра](requirements-register.md). **Код кандидата 0.7 прошел полный build-01 gate; весь v1 еще не принят. Production остается документированным выпуском 0.6.0-alpha.** GitHub gate, browser/NFR, целевая поставка, музыкальный пилот и телефон принимаются отдельно.

Матрица различает наличие кода, полученное доказательство и оставшуюся проверку. Процент готовности из числа unit-тестов не вычисляется. История прежних отказов и исправлений сохраняется в CHECK реестра и Git; эта текущая сверка не переписывает требования и не меняет current-release.

## Доказательства

Локальные ссылки на отчеты ведут за пределы Git в каталог оператора. Оригиналы build-01 лежат в `/home/av/build/phonoteka/reports/`. Тяжелые проверки выполнялись там, не на ноутбуке.

| ID | Подтверждено | Граница |
| --- | --- | --- |
| B79 | [a79ecdf-backend.xml](../../output/music-library/release-07/a79ecdf-backend.xml): **409 passed, 0 failed/error/skipped**, JUnit202.466с. Снимок `a79ecdf2e950`, run `20261018T194149Z-195506`; frontend и runtime61с прошли. | Чистый согласованный source-срез HEAD801981f. Проверяет синтетические/tempFS fixtures и контракты; не заменяет browser/NFR/device/live acceptance. Новое isolated demo8177 запущено, foreign container IDs сохранены; отдельный browser-review8178 еще не является протоколом взаимодействия. |
| CI79 | [GitHub dispatch39160004444](https://github.com/nd4y/phonoteka/actions/runs/39160004444) запущен; настоящая ASR-ветка прошла по текущему отчету оператора. | На момент среза весь workflow еще выполняется. Не объявлять app/inference image pair поставочным до полного success и проверки revision/IDs. |
| OLD | [J945 diagnostic](../../output/music-library/release-07/945959-context-diagnostic/diagnostic.xml):385/7; [J227](../../output/music-library/release-07/227c701-backend.xml):402/2; CI39158310594:399/5. | Исторические failed candidates; причины и fixes в CHECK-007/[разборе](../../output/music-library/release-07/ci-39158310594-failures.md). Не выданы за принятые образы. |
| FF6 | [Официальный Ubuntu24.04/FFmpeg6 эксперимент](../../output/music-library/release-07/ffmpeg6-warning-evidence.json) установил точную mono-layout note. | Остальные/fatal diagnostics не подавлены; соответствующие regressions теперь прошли B79. |
| PERF60 | [60-секундный smoke](../../output/music-library/release-07/performance-diagnostic-60s.json):10k строк,120HTTP,2 сессии,4 настоящих Worker/FFmpeg,2vCPU/1GiB; list/search/status p95 101.16/81.96/304.63мс. | Старый2277e0f; SSE p95 не прошел из-за poll2с. Исправление0.5с требует измерения. Не заменяет >=15/30 минут и1000HTTP. |
| P06 | [Исторический production GET](../../output/music-library/release-07/before-status.json):0.6.0-alpha,2153 записи, worker last_seen19:08:42Z. | Не свежий итог покрытия и не поставка0.7. Кандидат/попытка/завершенный HTTP не подтверждают правильность каждого трека. |

`B79: media53` в таблице означает 53 успешных testcase модуля `tests/test_media.py` в указанном JUnit. Это не 53 принятых AC. `SRC` означает чтение кода без отдельного выполненного приемочного сценария. Полный B79 закрывает программную регрессию перечисленных модулей именно на этом срезе; каждый составной AC дополнительно сверяется с колонкой «Осталось».

## Конечные обязательные этапы v1

Это приемка уже согласованного объема, без новых функций.

| Этап | Конкретно осталось | AC/CHG |
| --- | --- | --- |
| S1. Поставочный gate | Дождаться полного success CI79; проверить app+worker+inference revision/image IDs и испытанные артефакты. Локальный build-01 B79 уже зеленый; приемку production это не закрывает. | Общая граница поставки; CHG-010/031/032. |
| S2. Browser/PWA/наблюдаемость | Протокол реальных действий на390/768/1024/1440px: previous/next/filter/CUE/dirty guard, seek/lyrics, locks/album review; F5/PWA update сохраняют вкладку/позиции; keyboard/focus/reduced motion; install/offline/update/logout, private API/auth/media отсутствуют в caches; job colors/percent/ETA/actions/SSE reconnect без scroll jumps. После поставки свежие scrape/stdout/Grafana. | AC-19/22/47/53/54/74/84; CHG-008/016..025/027..029. |
| S3. Полный NFR/ресурсы | Раздел15:10k строк, FILE_WORKERS=4, суммарно app+worker2vCPU/4GiB, >=15минут и>=1000 list/search/statusHTTP,2UI/SSE сессии; каждый p95<=1с, lag p95<=100мс. Участок30минут без OOM/неограниченного RAM/fd/tasks роста, с child processes. Inference отдельно: actual CPU/RAM/PCM/temp/deadline и independent ordinary lane под нагрузкой. | AC-20/23/25..27/39/40/77. PERF60 закрывает только короткий smoke. |
| S4. Целевая установка | Controlled st1/internal HTTPS deploy, RO/disjoint mounts/UID/GID/storage guard/sync ACL, checkpoint/явная migration1->2, restart; отдельный restore stack с newer work/Ready и unavailable backup. Двойной первый bootstrap/owner sessions, direct HTTP/forwarded spoof/revoke/reset проверить на новой установке. TempFS fault cases B79 уже пройдены, но не доказывают реальные NAS mounts/ACL. | AC-01/02/11/16/24/41/42/44/57/65/67/69/80/82. Поставка и миграция еще не выполнены. |
| S5. Фактическая коллекция | Свежий full inventory/audit и смешанный review-пакет рабочей копии: каждый файл/CUE/WEBM учтен либо имеет причину; dry-run сохраняет fullSHA; tags/art/lyrics/errors/CSV/JSON сходятся с реестром. Проверить разрешенные metadata/fingerprint ветви и варианты записи. Не принуждать no_match к success и не обходить budgets. | AC-03..07/18/21/29..33/50..56/60/72. P06 счетчики не закрывают этап. |
| S6. Музыкальный пилот | Независимые30RU/EN песен:15+15,20studio+10сложных; >=10negative случаев, заранее зафиксированный человеческий эталон. Для>=70%studio покрытие>=90% всех эталонных строк при median<=300мс/p95<=800мс; результаты/отказы по песне и языку. Непринятые строки остаются partial. | AC-30/34..38/76. [Протокол](musical-pilot.md) есть, выполненного отчета нет. `PILOT_REGISTRY={}`, automatic default off, `pilot_not_validated` сохраняются. |
| S7. Android/Poweramp | >=10MP3+10FLAC реально доставлены и проверены offline: tags/front cover/строки в начале/середине/конце/после seek, UnicodeM3U8, Track/AlbumGain; one-way RO transport, partial/resume/ENOSPC, новая revision с прежним именем и независимые device stamps. | AC-17/55/57/61..64/79. Генератор `dev/poweramp_fixtures.py` и пустой result.csv не заменяют телефон. |

На этом срезе подтвержденных отсутствующих обязательных backend-маршрутов не установлено. Это не обещание отсутствия дефектов или закрытия S1..S7. Новые отказы фиксируются относительно существующего AC, без бесконечного расширения объема. Физический power-cut NAS не объявляется выполненным и не добавляется молча как новая обязательная операция.

## Матрица AC-01..84

Все исходные номера сохранены. Названия реализации - модули `backend/phonoteka`, если не указано иное. S1 остается общей поставочной границей всех строк; ниже дополнительно указаны специфические остатки.

| AC | Реализовано в HEAD | Проверено | Осталось |
| --- | --- | --- | --- |
| AC-01 | Compose/non-root/persistent state | Историческая P06; smoke B79 | S4 новый NAS restart. |
| AC-02 | media roots/no-links/no-original-write | B79:media53/fault14 | S4 mounts/ACL; S5 SHA. |
| AC-03 | worker/intake/signatures/CUE/invalid rows | B79:backend27/CUE20/hash3 | S5 свежий полный каталог. |
| AC-04 | Read-only analysis/dry-run preview | B79:backend | S5 before/after fullSHA. |
| AC-05 | Native unknown tags/multiple slot preservation | B79:media53/slots15 | S5 реальный corpus/кириллица. |
| AC-06 | Variant/mix conflict, no automatic acceptance | B79:providers11/quality23 | S5/S6 live/remix/cover. |
| AC-07 | Native write/reopen/packet hash/selected front | B79:media53/slots15/canonical_sidecars1 | S5 corpus, S7 device. |
| AC-08 | LRC equal times/offset/error validation | B79:media53/lyrics_document6 | S2 import/редактор. |
| AC-09 | Plain/partial имеет nullable times, не synced | B79:backend/lyrics_document6 | S2 понятное состояние UI. |
| AC-10 | Prepared journal/before-after recovery/DB reconciliation | B79:fault14/media53 | Программная fault matrix пройдена; S4 поставка на целевую FS. |
| AC-11 | Aggregate space reserve/RO/missing/error | B79:fault14/storage_wait2 | S4 RO/lost mount target. |
| AC-12 | Bounded timeout/retry/backoff/cooldown | B79:providers11/resume2 | S3 поведение под нагрузкой. |
| AC-13 | Immutable proposals/idempotency per principal | B79:API6/principals3/backend | S2 двойное UI действие. |
| AC-14 | SHA CAS enqueue/stage/effect, external conflict | B79:backend/media/fault | S4 права независимых писателей. |
| AC-15 | Work/Ready rollback, backup/hash/CAS | B79:publication4/fault/slots | S2 UI; S4 целевой restore. |
| AC-16 | Separate restore/dependencies/recovery mode/revoked credentials | B79:maintenance6/fault14 | S4 отдельный stack. |
| AC-17 | Embedded tags/cover/SYLT/Vorbis + device package | B79 writer; generator SRC | S7 физические20 файлов; не принято. |
| AC-18 | Actual file outcomes/reasons/sources/review | B79:reports2/backend | S5 mixed real package. |
| AC-19 | Responsive themes/dock/focus/motion/keyboard | Frontend gate B79 | S2 четыре viewport/действия. |
| AC-20 | Pagination/search/limited workers/backpressure | PERF60:10k/120HTTP/4workers | S3 полный нормированный профиль. |
| AC-21 | CSV/JSON actual results/provenance/exclusions/formula escaping | B79:reports2/backend | S5 реальные счетчики. |
| AC-22 | Local editor/history/rollback без providers | B79:backend/catalogs2 | S2 offline core UI. |
| AC-23 | Async lanes/executor/real parallel files | PERF60 threads; B79:fault/backend | S3 I/O+network overlap trace. |
| AC-24 | File lock/CAS; Gain lock до catalog persist | B79:fault14/group_gain7 | S4 storage-domain stack concurrency. |
| AC-25 | API отдельно от file/inference lane; lag metrics | PERF60 HTTP p95<1с | S3 >=15m/1000 и30m resources. |
| AC-26 | Semaphore-before-throttle/shared budget/deadline | B79:providers/resume/attempts4 | S3 slow-source fairness/rate trace. |
| AC-27 | Bounded queue/tasks/executor, streaming decode/hash | B79:backend; PERF60 RAM/fd | S3 10k queue/30m parent+children. |
| AC-28 | Pause/shutdown drain in-flight, guard удержан | B79:fault14/backend | S4 controlled ownership handoff. |
| AC-29 | Free-first ShazamIO; recording/release разделены | B79:recognition6/budget4/audit3 | S5 actual music. ACR/AudD без ключей не требуются по CHG-013. |
| AC-30 | Ambiguous mix/variant/wrong text видимы, не autoaccept | B79:recognition/providers/quality/align | S5/S6 реальные negatives. |
| AC-31 | no_match/401/429/timeout/budget различимы | B79:attempts4/budget4/recognition | S3 лимиты полной очереди. |
| AC-32 | Audio-identity cache переживает tags/new batch | B79:recognition/budget4 | S5 реальный tag-only цикл. |
| AC-33 | Consent/fingerprint samples; metadata audio не отправляет | B79:recognition/audit/providers | S5 actual requests + unchangedSHA. |
| AC-34 | CPU alignment + протокол метрик | CI speech smoke; пилота нет | S6 обязательные30 песен. |
| AC-35 | Independent ASR/wrong-text/unaligned gates | B79:alignment3/quality23; CI smoke | S6 repeats/silence/instrumental negatives. |
| AC-36 | Timing review global shift/local drift, owner draft | B79:alignment; UI source | S2/S6 actual offset/drift. |
| AC-37 | Resample/crop windows -> original timeline, work unchanged | B79:alignment; CI speech | S6 шкала времени. Vocal separation выключен. |
| AC-38 | ASR draft/partial/unsupported language/nullable | B79:alignment/budget14/lyrics | S6 языки и отказы. |
| AC-39 | Independent inference lane/dedup/deadlines/isolated child | B79:backend/inference_budget14; CI model | S3 реальный OOM/restart/API/resource profile. |
| AC-40 | Configured CPU transcribe/align/model contract | Настоящая CI79 ASR-ветка | S3/S6 CPU ресурсы/контроль; GPU/remote условны и выключены. |
| AC-41 | Fixed owner/password file/bootstrap/sessions | B79:backend auth | S4 двойной bootstrap/multi-session. |
| AC-42 | Cookie/CSRF/origin/private media, public-only SW | B79:backend/exports; PWA tests | S2 cache/logout; S4 proxy/direct HTTP. |
| AC-43 | Scoped analysis/write tokens + worker/automatic gates | B79:automation3/quality23 | S2/S4 token boundary. |
| AC-44 | Logout-all/token revoke/local reset/epoch | B79:backend/automation | S2/S4 actual new sessions/reset. |
| AC-45 | OIDC отсутствует/не включен; local owner есть | Config/docs | Условный optional AC; не новый core модуль. |
| AC-46 | Disabled TheAudioDB/Discogs adapters/limits/normalization | B79:catalogs2 | Live keyed профиль только при настройке/правах. |
| AC-47 | Field locks/priorities/manual first/CAS, words/timing locks | B79:field_policy7/lyrics_document6 | S2 owner workflow. |
| AC-48 | Fetch/cache/embed/export/analysis rights/TTL/transient/inheritance | B79:policy9/artwork7/canonical1 | S2/S5 provenance; S4 restore/export. |
| AC-49 | Core независим от commercial/n8n/OIDC/Spotify/Apple | B79:disabled providers/core | S2 honest disabled/not configured UI. |
| AC-50 | All-stream audit before fingerprint/Ready; bad file state | B79:multistream2/audit3/media | S5 actual damaged/cut corpus. |
| AC-51 | Silence/sample peak/possible clipping warnings; lossless unknown | B79:media53 + FF6 evidence | S5 реальные примеры; нет необоснованного удаления. |
| AC-52 | Exact/packet/candidate groups, A-B/format facts | B79:comparison4/reports/backend | S2 A-B; S5 разные исполнения. |
| AC-53 | Versioned preferred/both/defer, explicit aliases/no deletion | B79:comparison4/exports4 | S2; S7 playlist/device. |
| AC-54 | Exact MB release/discs/IDs/duration/unknown/subset; batch proposals | B79:release7/album_batch9 | S2; S5 allowed live release/corpus. |
| AC-55 | Track/joint Gain, immutable group/audio/release CAS | B79:loudness8/album5/group7 | S5 состав; S7 Poweramp Gain. |
| AC-56 | Accepted FileRevision/lyrics_required/instrumental/relaxed/frozen gate | B79:lyrics_review9/publication/API | S2/S5 реальный accepted/ASR-negative пакет. |
| AC-57 | Disjoint/no-hardlink/copy-to-Ready/separate sync rights | B79:media/backend; historical compose | S4 real ACL; S7 sync account. |
| AC-58 | Object journals/recovery/immutable generation; partial != atomic batch | B79:publication4/sidecars12/canonical1 | S2 partial states; S4 target update. |
| AC-59 | Frozen Ready CAS/named plan/commit/independent rollback | B79:publication4/API6 | S2; S4 external target conflict. |
| AC-60 | Relative UTF-8 M3U8/verified targets/aliases/NFC-case collision | B79:exports4/sidecars/canonical1 | S5/S7 playlist. Общий импорт внешних playlists не предписан. |
| AC-61 | One-way RO exports/receipt contract, no hidden device control | B79:exports; automation docs | S4 sync ACL, S7 actual client. |
| AC-62 | Immutable files/Range/leases/no unproved checksum | B79:exports4/retention4 | S7 partial/resume/ENOSPC клиента. |
| AC-63 | Published/transferred/verified/Poweramp states разделены | B79:exports; device generator | S7 offline телефон; не принято. |
| AC-64 | Receipt bound export/fileSHA; newrevision invalidates evidence | B79:exports4 | S7 новая доставка с прежним именем. |
| AC-65 | Shared guard/disjoint/alias/link/lost-root protection | B79:backend/fault14 | S4 two stacks/mounts/SMB ACL. |
| AC-66 | Aggregate reserve/backup/fsync/rename/DB failure reconciliation | B79:fault14/media53/storage2 | Программная fault matrix пройдена; не выдается за отказ питания NAS. |
| AC-67 | Restored-state recovery/newer media conflict/unavailable blob | B79:fault/maintenance6/migrations9 | S4 отдельный restore stack, не live DB replace. |
| AC-68 | Keep-all/pins/leases/version graph/CAS prune/waiting_storage | B79:retention4/storage2 | S4 isolated эксплуатационный контроль; production prune не выполнялся. |
| AC-69 | Epoch/token effect checks; logout не отзывает approved job | B79:automation3/backend/principals3 | S4 actual revoke/expiry/reset boundaries. |
| AC-70 | Durable UsageAttempt/admission/unknown budget/no hidden retry | B79:attempts4/budget4 | Старые calls до ledger не реконструируются; S3 runtime limits. |
| AC-71 | FullSHA/date/separate IDs/history/partial scan no-delete | B79:hash3/fault14 | S5 fresh real copies/move/hash pass. |
| AC-72 | Missing keys/wait/skipped/cancelled_partial/actual counters | B79:resume/backend/activity10/reports | S2 job UI, S5 actual queue итог. |
| AC-73 | Principal idempotency/immutable proposals/plans/ETag/frozen deps | B79:principals3/API6/review/quality | S2 repeat/double action. |
| AC-74 | SSE retained IDs/resync; optional outbox/HMAC/durable retry | B79:activity10/webhooks10 | S2 SSE; webhook runtime условен по R4 и сейчас не настроен. |
| AC-75 | Durable lyric IDs/nullable/BOM/offset/occurrences/metadata | B79:lyrics_document6/slots15 | S2 edit/import. |
| AC-76 | One server automatic gate/reasons/fresh deps/auth/locks | B79:quality23 | S6 pilot обязателен; `pilot_not_validated` не обходится UI/token. |
| AC-77 | Model/PCM/temp/result budgets и isolated watchdog | B79:inference_budget14; CI actual model | S3 resident/long input/OOM profile. |
| AC-78 | Provider DNS/redirect guards/no playlist URL decode/text-only/CSV escape | B79:providers/media/backend/reports/webhooks | S4 fixed inference/proxy profile. |
| AC-79 | ID3v2.3/v2.4/native slots/unknown data, unsupported RO | B79:media53/slots15 | S5/S7 corpus/gapless. |
| AC-80 | Explicit checkpoint migration1->2/failure marker/legacy write barriers | B79:migrations9 | S4 actual migration/restore, еще не выполнены. |
| AC-81 | Frozen sidecar owners/group CAS/journals/immutable export manifest | B79:sidecars12/canonical1 | S2 plan/partial UI; multi-file atomic не заявлен. |
| AC-82 | HTTPS origin/Secure/CSRF/no forwarded-trust fallback | B79:backend/settings; historical proxy | S4 current HTTPS/direct/spoof protocol. |
| AC-83 | Legacy/owner/provider origins/known restrictions through derivatives | B79:policy9/artwork7/canonical1 | S2/S5 visible provenance; S4 backup/export. |
| AC-84 | Original timeline/CUE bounds/stable draft/undo/batch per-file CAS | B79:navigation3/CUE/album_batch9 | S2 real seek/dirty/batch conflict; S6 timing. |

## Сверка CHG-001..032

Полные формулировки и история остаются в реестре. Эта таблица не устанавливает новых требований.

| CHG | Реализовано/состояние | Осталось |
| --- | --- | --- |
| CHG-001 | Tags/cover/embedded timed lyrics | S5/S6/S7. |
| CHG-002 | Separate work, RO original | S4/S5. |
| CHG-003 | Самостоятельный Compose/API/worker, n8n optional | S1/S4. |
| CHG-004 | Bounded async/thread lanes | S3. |
| CHG-005 | Actual ASR/align + line review/server gate | S6 musical quality. |
| CHG-006 | Free providers/priority/rights; disabled optional catalogs | S5; keyed access только при настройке. |
| CHG-007 | Single owner/sessions/CSRF/tokens/logout-all/reset | S4 security приемка. |
| CHG-008 | Material3 Expressive/темы/макеты | S2. |
| CHG-009 | Архивы ТЗ/review/84AC сохранены | Финальная сверка, требований не удаляли. |
| CHG-010 | Историческая GitHub/st1/internal proxy поставка0.6 | S1/S4 нового кандидата. |
| CHG-011 | CUE/INDEX/encodings/PCM derivatives | S2/S5. |
| CHG-012 | Explicit formats/errors/RO/normalization | S5 corpus, без обещания ремонта любой порчи. |
| CHG-013 | Free-first вместо paid adapters без ключей | Соблюдено; уточняет AC-29. |
| CHG-014 | Consent/shared budgets/retry/cache | S5 fresh coverage report. |
| CHG-015 | YouTube evidence/search/owner link без media downloads | B79:youtube23; S5. |
| CHG-016 | Dedicated metrics/JSON stdout/action codes | B79:observability12; S4 ingestion. |
| CHG-017 | Grafana47 panels/Prometheus/VictoriaLogs | S4 live scrape/panels/logs, не только import. |
| CHG-018 | Basic player transport/seek/rate/volume/keyboard/CUE | S2. |
| CHG-019 | Previous/next/server filtered order/CUE | B79:navigation3; S2 dirty/playing/paused. |
| CHG-020 | Persistent bottom dock/mobile | S2. |
| CHG-021 | PWA manifest/install/update/public-only cache | B79 frontend/PWA gate; S2 lifecycle. |
| CHG-022 | Animations/reduced motion | S2 actions/focus. |
| CHG-023 | View/filter/track/scroll persistence/dirty guard | S2 F5/async/PWA update. |
| CHG-024 | Прежний дизайн доработан, обе темы/mobile | S2 visual/interactive. |
| CHG-025 | Непонятная плашка копии удалена; source safety сохранена | S2 current bundle. |
| CHG-026 | [Источники файлов](file-sources.md) проработаны | Только проектирование; cloud/device adapters не реализовывать. |
| CHG-027 | Completed/cancelled/error различаются текстом/иконкой/цветом | S2. |
| CHG-028 | Percent/counts/honest active-window ETA | B79:activity10; S2/S3 wait/paused/unknown. |
| CHG-029 | Current actions/SSE/resync/bounded log view | B79:activity10; S2/S3 latency/no forced scroll. |
| CHG-030 | Сохраняемый реестр + полный текущий audit | Финальный CHECK после результатов S1..S7. |
| CHG-031 | Heavy build/test/demo на build-01/operation lock/изоляция | B79 green; ноутбук source/browser only. |
| CHG-032 | Завершить согласованный v1 и уведомить | Открыто S1..S7; «ТЗ выполнено» пока преждевременно. |

## Явная граница объема

Cloud/S3/Google/Yandex/Mail.ru/device file-source adapters остаются проектированием CHG-026. OIDC, GPU/remote и коммерческие/keyed профили принимаются при явной настройке; они не включаются ради отметки в таблице. Webhook delivery/n8n условны по R4 раздела16; core API/SSE обязательны. AC-60 требует переносимый экспорт и сохранность исходных плейлистов; общий внешний import/Spotify/Apple Music не превращены в core v1.

Отсутствие музыкального эталона или телефона не разрешает автоматически принять соответствующие AC. Одновременно не требуется гарантированно распознать неизвестную запись или восстановить любой поврежденный файл: нужен честный результат, сохранность данных и доступный ручной путь. Полное уведомление возможно только после результатов применимых S1..S7.
