# Сверка ТЗ v1.4: реализация и приемка

Проверенный базовый срез: `35b020fe6262cdd9f38c057d95a3b01107b82f5a`, 19.10.2026. Основание - все 84 AC [исходного ТЗ](specification.md) и CHG-001..032 [реестра](requirements-register.md). **Этот кандидат 0.7 прошел чистый build-01 и полный GitHub gate; плеер, обработка и PWA проверены в браузере на build-01. Весь v1 еще не принят. Последний подтвержденный production остается 0.6.0-alpha.** При проверке AC-19 обнаружены мобильные touch targets меньше44px; CSS исправляется и требует нового frontend/GitHub gate. Длительный NFR, целевая поставка, музыкальный пилот и телефон принимаются отдельно.

Матрица различает наличие кода, полученное доказательство и оставшуюся проверку. Процент готовности из числа unit-тестов не вычисляется. История прежних отказов и исправлений сохраняется в CHECK реестра и Git; эта текущая сверка не переписывает требования и не меняет current-release.

## Доказательства

Локальные ссылки на отчеты ведут за пределы Git в каталог оператора. Оригиналы build-01 лежат в `/home/av/build/phonoteka/reports/`. Тяжелые проверки выполнялись там, не на ноутбуке.

| ID | Подтверждено | Граница |
| --- | --- | --- |
| BCCA | [cca85c5-backend.xml](../../output/music-library/release-07/cca85c5-backend.xml): **409 passed, 0 failed/error/skipped**, JUnit180.545с. Чистый снимок `cca85c5e816809f5d5997d04adb0766d4f0e82d01bbafec03f2a8ef072b18a85`, run `20261018T210058Z-247716`; TS/Vite/runtime, **52 frontend tests/13 files**, включая7 PWA lifecycle regressions, прошли. | Базовый source-срез HEAD35b020f перед CSS исправлением touch targets. Синтетические/tempFS fixtures и контракты; не заменяет NFR/device/music acceptance. Старый B79:409/0 сохраняется отдельным историческим отчетом. |
| CI35 | [GitHub dispatch39166290627](https://github.com/nd4y/phonoteka/actions/runs/39166290627): **completed/success**, revision35b020f; app/inference прошли, включая настоящую ASR/alignment/negative/dedup. | [App](../../output/music-library/release-07/loaded-image.json) `f93e121e2a60` и [inference](../../output/music-library/release-07/loaded-inference.json) `1c326634342b` загружены с проверкой archive/artifact SHA и одинаковой OCI revision. App также проверен на build-01. Загрузка образов не равна deployment. |
| UI35 | [Протокол действий CUA](../../output/music-library/release-07/browser-functional-evidence.json), [responsive](../../output/music-library/release-07/responsive-player.json), [PWA](../../output/music-library/release-07/pwa-browser-evidence.json): фильтр/соседи/dirty/error/seek/lyrics/CUE; live ETA/pause/cancel; 390/768/1024/1440px, dark/light; F5/update сохраняют контекст. | Обычный owner login, только generated fixtures. PWA server-unavailable проверен остановкой своего review app, не физическим отключением сети. Android install/Poweramp не проверены. Это протокол взаимодействий, а не вывод о функциональности из скриншота. |
| NAS35 | [Storage acceptance](../../output/music-library/release-07/storage-acceptance-latest.json): реальная volume2/volume3, shared writer guard, reopen tags/cover/lyrics, packet preservation, committed verified backup; официальный snapshot/restore при реально более новых work/Ready удержал recovery gate и сохранил файлы. | Отдельные generated private files; production mounts отсутствовали, музыка production не копировалась, identity production services сохранены. Это проверка целевой FS и recovery, не миграция production DB и не подтверждение sync-account ACL. |
| OLD | [J945 diagnostic](../../output/music-library/release-07/945959-context-diagnostic/diagnostic.xml):385/7; [J227](../../output/music-library/release-07/227c701-backend.xml):402/2; CI39158310594:399/5. | Исторические failed candidates; причины и fixes в CHECK-007/[разборе](../../output/music-library/release-07/ci-39158310594-failures.md). Не выданы за принятые образы. |
| FF6 | [Официальный Ubuntu24.04/FFmpeg6 эксперимент](../../output/music-library/release-07/ffmpeg6-warning-evidence.json) установил точную mono-layout note. | Остальные/fatal diagnostics не подавлены; соответствующие regressions теперь прошли BCCA. |
| PERF60 | [60-секундный release smoke](../../output/music-library/release-07/performance-release-60s.json) на GitHub app image35b020f:10k строк,120HTTP,2 сессии,4 настоящих Worker/FFmpeg,2vCPU/1GiB; list/search/status p95 **85.82/90.00/333.47мс**, lag p95 **26.48мс**, SSE p95 **531.12/519.73мс**; все measured checks прошли. | 220 updates/15 bounded batches, max4 concurrent files, max progress gap2.09с; resources учитывают child processes. Старый failed SSE smoke сохранен. Этот успешный короткий smoke не заменяет >=15/30 минут и1000HTTP; inference/providers в нем не участвуют. |
| P06 | [Исторический production GET](../../output/music-library/release-07/before-status.json):0.6.0-alpha,2153 записи, worker last_seen19:08:42Z. | Не свежий итог покрытия и не поставка0.7. Кандидат/попытка/завершенный HTTP не подтверждают правильность каждого трека. |

`BCCA: media53` в таблице означает 53 успешных testcase модуля `tests/test_media.py` в указанном JUnit. Это не 53 принятых AC. `SRC` означает чтение кода без отдельного выполненного приемочного сценария. Полный BCCA закрывает программную регрессию перечисленных модулей именно на этом срезе; каждый составной AC дополнительно сверяется с колонкой «Осталось».

## Конечные обязательные этапы v1

Это приемка уже согласованного объема, без новых функций.

| Этап | Конкретно осталось | AC/CHG |
| --- | --- | --- |
| S1. Поставочный gate | Базовый CI35 и matched app/worker/inference image pair проверены, BCCA зеленый. После CSS исправления mobile touch targets требуется новый frontend/GitHub gate и привязка итоговых revision/IDs; не выдавать35b за окончательный релиз. | CHG-010/031/032. Не означает production-поставку. |
| S2. Browser/PWA/наблюдаемость | UI35 подтвердил player/filter/CUE/dirty/error/seek/lyrics, mobile/themes, refresh/update, live percent/ETA/actions/pause/cancel. Исправить и повторно измерить touch targets>=44px. Остались отдельные owner workflows locks/album review/A-B/sidecar, keyboard/focus/reduced-motion и logout/cache/reconnect edge cases; после поставки свежие scrape/stdout/Grafana. Android installation относится к S7; физический network-offline не проверен. | AC-19/22/47/53/54/74/84; CHG-008/016..025/027..029. |
| S3. Полный NFR/ресурсы | Раздел15:10k строк, FILE_WORKERS=4, суммарно app+worker2vCPU/4GiB, >=15минут и>=1000 list/search/statusHTTP,2UI/SSE сессии; каждый p95<=1с, lag p95<=100мс. Участок30минут без OOM/неограниченного RAM/fd/tasks роста, с child processes. Inference отдельно: actual CPU/RAM/PCM/temp/deadline и independent ordinary lane под нагрузкой. | AC-20/23/25..27/39/40/77. PERF60 закрывает только короткий smoke. |
| S4. Целевая установка | NAS35 уже подтвердил shared guard и отдельный official restore при newer work/Ready на настоящей FS. Остались controlled st1/internal HTTPS deploy, RO/disjoint production mounts/UID/GID/sync ACL, checkpoint/явная migration1->2/restart и проверка недоступного backup; auth/direct HTTP/forwarded spoof/revoke/reset boundaries новой поставки. Production DB не подменять тестовым restore. | AC-01/02/11/16/24/41/42/44/57/65/67/69/80/82. Поставка и миграция еще не выполнены. |
| S5. Фактическая коллекция | Свежий full inventory/audit и смешанный review-пакет рабочей копии: каждый файл/CUE/WEBM учтен либо имеет причину; dry-run сохраняет fullSHA; tags/art/lyrics/errors/CSV/JSON сходятся с реестром. Проверить разрешенные metadata/fingerprint ветви и варианты записи. Не принуждать no_match к success и не обходить budgets. | AC-03..07/18/21/29..33/50..56/60/72. P06 счетчики не закрывают этап. |
| S6. Музыкальный пилот | Независимые30RU/EN песен:15+15,20studio+10сложных; >=10negative случаев, заранее зафиксированный человеческий эталон. Для>=70%studio покрытие>=90% всех эталонных строк при median<=300мс/p95<=800мс; результаты/отказы по песне и языку. Непринятые строки остаются partial. | AC-30/34..38/76. [Протокол](musical-pilot.md) есть, выполненного отчета нет. `PILOT_REGISTRY={}`, automatic default off, `pilot_not_validated` сохраняются. |
| S7. Android/Poweramp | >=10MP3+10FLAC реально доставлены и проверены offline: tags/front cover/строки в начале/середине/конце/после seek, UnicodeM3U8, Track/AlbumGain; one-way RO transport, partial/resume/ENOSPC, новая revision с прежним именем и независимые device stamps. | AC-17/55/57/61..64/79. Генератор `dev/poweramp_fixtures.py` и пустой result.csv не заменяют телефон. |

На этом срезе подтвержденных отсутствующих обязательных backend-маршрутов не установлено. Это не обещание отсутствия дефектов или закрытия S1..S7. Новые отказы фиксируются относительно существующего AC, без бесконечного расширения объема. Физический power-cut NAS не объявляется выполненным и не добавляется молча как новая обязательная операция.

## Матрица AC-01..84

Все исходные номера сохранены. Названия реализации - модули `backend/phonoteka`, если не указано иное. S1 остается общей поставочной границей всех строк; ниже дополнительно указаны специфические остатки.

| AC | Реализовано в HEAD | Проверено | Осталось |
| --- | --- | --- | --- |
| AC-01 | Compose/non-root/persistent state | Историческая P06; smoke BCCA | S4 новый NAS restart. |
| AC-02 | media roots/no-links/no-original-write | BCCA:media53/fault14 | S4 mounts/ACL; S5 SHA. |
| AC-03 | worker/intake/signatures/CUE/invalid rows | BCCA:backend27/CUE20/hash3 | S5 свежий полный каталог. |
| AC-04 | Read-only analysis/dry-run preview | BCCA:backend | S5 before/after fullSHA. |
| AC-05 | Native unknown tags/multiple slot preservation | BCCA:media53/slots15 | S5 реальный corpus/кириллица. |
| AC-06 | Variant/mix conflict, no automatic acceptance | BCCA:providers11/quality23 | S5/S6 live/remix/cover. |
| AC-07 | Native write/reopen/packet hash/selected front | BCCA:media53/slots15/canonical_sidecars1 | S5 corpus, S7 device. |
| AC-08 | LRC equal times/offset/error validation | BCCA:media53/lyrics_document6; UI35 lyric click/seek5с | S2 import/редактор. |
| AC-09 | Plain/partial имеет nullable times, не synced | BCCA:backend/lyrics_document6 | S2 понятное состояние UI. |
| AC-10 | Prepared journal/before-after recovery/DB reconciliation | BCCA:fault14/media53 | Программная fault matrix пройдена; S4 поставка на целевую FS. |
| AC-11 | Aggregate space reserve/RO/missing/error | BCCA:fault14/storage_wait2 | S4 RO/lost mount target. |
| AC-12 | Bounded timeout/retry/backoff/cooldown | BCCA:providers11/resume2 | S3 поведение под нагрузкой. |
| AC-13 | Immutable proposals/idempotency per principal | BCCA:API6/principals3/backend | S2 двойное UI действие. |
| AC-14 | SHA CAS enqueue/stage/effect, external conflict | BCCA:backend/media/fault | S4 права независимых писателей. |
| AC-15 | Work/Ready rollback, backup/hash/CAS | BCCA:publication4/fault/slots; NAS35 real backup/restore | S2 UI; S4 production deploy/recovery boundaries. |
| AC-16 | Separate restore/dependencies/recovery mode/revoked credentials | BCCA:maintenance6/fault14; NAS35 separate official restore/newer media | S4 production migration/auth boundaries; missing blob. |
| AC-17 | Embedded tags/cover/SYLT/Vorbis + device package | B79 writer; generator SRC | S7 физические20 файлов; не принято. |
| AC-18 | Actual file outcomes/reasons/sources/review | BCCA:reports2/backend | S5 mixed real package. |
| AC-19 | Responsive themes/dock/focus/motion/keyboard | Frontend gate BCCA; UI35 390/768/1024/1440px dark/light, player/jobs | S2 исправить/измерить touch targets>=44px, keyboard/focus/reduced motion. |
| AC-20 | Pagination/search/limited workers/backpressure | PERF60:10k/120HTTP/4workers | S3 полный нормированный профиль. |
| AC-21 | CSV/JSON actual results/provenance/exclusions/formula escaping | BCCA:reports2/backend | S5 реальные счетчики. |
| AC-22 | Local editor/history/rollback без providers | BCCA:backend/catalogs2 | S2 offline core UI. |
| AC-23 | Async lanes/executor/real parallel files | PERF60 threads; BCCA:fault/backend | S3 I/O+network overlap trace. |
| AC-24 | File lock/CAS; Gain lock до catalog persist | BCCA:fault14/group_gain7; NAS35 shared guard blocks second writer | S4 production storage-domain/mount binding. |
| AC-25 | API отдельно от file/inference lane; lag metrics | PERF60 HTTP p95<1с | S3 >=15m/1000 и30m resources. |
| AC-26 | Semaphore-before-throttle/shared budget/deadline | BCCA:providers/resume/attempts4 | S3 slow-source fairness/rate trace. |
| AC-27 | Bounded queue/tasks/executor, streaming decode/hash | BCCA:backend; PERF60 RAM/fd | S3 10k queue/30m parent+children. |
| AC-28 | Pause/shutdown drain in-flight, guard удержан | BCCA:fault14/backend | S4 controlled ownership handoff. |
| AC-29 | Free-first ShazamIO; recording/release разделены | BCCA:recognition6/budget4/audit3 | S5 actual music. ACR/AudD без ключей не требуются по CHG-013. |
| AC-30 | Ambiguous mix/variant/wrong text видимы, не autoaccept | BCCA:recognition/providers/quality/align | S5/S6 реальные negatives. |
| AC-31 | no_match/401/429/timeout/budget различимы | BCCA:attempts4/budget4/recognition | S3 лимиты полной очереди. |
| AC-32 | Audio-identity cache переживает tags/new batch | BCCA:recognition/budget4 | S5 реальный tag-only цикл. |
| AC-33 | Consent/fingerprint samples; metadata audio не отправляет | BCCA:recognition/audit/providers | S5 actual requests + unchangedSHA. |
| AC-34 | CPU alignment + протокол метрик | CI speech smoke; пилота нет | S6 обязательные30 песен. |
| AC-35 | Independent ASR/wrong-text/unaligned gates | BCCA:alignment3/quality23; CI smoke | S6 repeats/silence/instrumental negatives. |
| AC-36 | Timing review global shift/local drift, owner draft | BCCA:alignment; UI source | S2/S6 actual offset/drift. |
| AC-37 | Resample/crop windows -> original timeline, work unchanged | BCCA:alignment; CI speech | S6 шкала времени. Vocal separation выключен. |
| AC-38 | ASR draft/partial/unsupported language/nullable | BCCA:alignment/budget14/lyrics | S6 языки и отказы. |
| AC-39 | Independent inference lane/dedup/deadlines/isolated child | BCCA:backend/inference_budget14; CI model | S3 реальный OOM/restart/API/resource profile. |
| AC-40 | Configured CPU transcribe/align/model contract | Настоящая CI35 ASR-ветка | S3/S6 CPU ресурсы/контроль; GPU/remote условны и выключены. |
| AC-41 | Fixed owner/password file/bootstrap/sessions | BCCA:backend auth | S4 двойной bootstrap/multi-session. |
| AC-42 | Cookie/CSRF/origin/private media, public-only SW | BCCA:backend/exports/PWA7; UI35 first install/update/server-unavailable public shell | S2 logout/cache edge cases; S4 proxy/direct HTTP. Physical offline не проверен. |
| AC-43 | Scoped analysis/write tokens + worker/automatic gates | BCCA:automation3/quality23 | S2/S4 token boundary. |
| AC-44 | Logout-all/token revoke/local reset/epoch | BCCA:backend/automation | S2/S4 actual new sessions/reset. |
| AC-45 | OIDC отсутствует/не включен; local owner есть | Config/docs | Условный optional AC; не новый core модуль. |
| AC-46 | Disabled TheAudioDB/Discogs adapters/limits/normalization | BCCA:catalogs2 | Live keyed профиль только при настройке/правах. |
| AC-47 | Field locks/priorities/manual first/CAS, words/timing locks | BCCA:field_policy7/lyrics_document6 | S2 owner workflow. |
| AC-48 | Fetch/cache/embed/export/analysis rights/TTL/transient/inheritance | BCCA:policy9/artwork7/canonical1 | S2/S5 provenance; S4 restore/export. |
| AC-49 | Core независим от commercial/n8n/OIDC/Spotify/Apple | BCCA:disabled providers/core | S2 honest disabled/not configured UI. |
| AC-50 | All-stream audit before fingerprint/Ready; bad file state | BCCA:multistream2/audit3/media | S5 actual damaged/cut corpus. |
| AC-51 | Silence/sample peak/possible clipping warnings; lossless unknown | BCCA:media53 + FF6 evidence | S5 реальные примеры; нет необоснованного удаления. |
| AC-52 | Exact/packet/candidate groups, A-B/format facts | BCCA:comparison4/reports/backend | S2 A-B; S5 разные исполнения. |
| AC-53 | Versioned preferred/both/defer, explicit aliases/no deletion | BCCA:comparison4/exports4 | S2; S7 playlist/device. |
| AC-54 | Exact MB release/discs/IDs/duration/unknown/subset; batch proposals | BCCA:release7/album_batch9 | S2; S5 allowed live release/corpus. |
| AC-55 | Track/joint Gain, immutable group/audio/release CAS | BCCA:loudness8/album5/group7 | S5 состав; S7 Poweramp Gain. |
| AC-56 | Accepted FileRevision/lyrics_required/instrumental/relaxed/frozen gate | BCCA:lyrics_review9/publication/API | S2/S5 реальный accepted/ASR-negative пакет. |
| AC-57 | Disjoint/no-hardlink/copy-to-Ready/separate sync rights | BCCA:media/backend; historical compose | S4 real ACL; S7 sync account. |
| AC-58 | Object journals/recovery/immutable generation; partial != atomic batch | BCCA:publication4/sidecars12/canonical1 | S2 partial states; S4 target update. |
| AC-59 | Frozen Ready CAS/named plan/commit/independent rollback | BCCA:publication4/API6 | S2; S4 external target conflict. |
| AC-60 | Relative UTF-8 M3U8/verified targets/aliases/NFC-case collision | BCCA:exports4/sidecars/canonical1 | S5/S7 playlist. Общий импорт внешних playlists не предписан. |
| AC-61 | One-way RO exports/receipt contract, no hidden device control | BCCA:exports; automation docs | S4 sync ACL, S7 actual client. |
| AC-62 | Immutable files/Range/leases/no unproved checksum | BCCA:exports4/retention4 | S7 partial/resume/ENOSPC клиента. |
| AC-63 | Published/transferred/verified/Poweramp states разделены | BCCA:exports; device generator | S7 offline телефон; не принято. |
| AC-64 | Receipt bound export/fileSHA; newrevision invalidates evidence | BCCA:exports4 | S7 новая доставка с прежним именем. |
| AC-65 | Shared guard/disjoint/alias/link/lost-root protection | BCCA:backend/fault14; NAS35 blocks second writer on real FS | S4 production mounts/SMB/sync ACL. |
| AC-66 | Aggregate reserve/backup/fsync/rename/DB failure reconciliation | BCCA:fault14/media53/storage2 | Программная fault matrix пройдена; не выдается за отказ питания NAS. |
| AC-67 | Restored-state recovery/newer media conflict/unavailable blob | BCCA:fault/maintenance6/migrations9; NAS35 actual newer work/Ready preserved | S4 missing backup runtime boundary; live DB не заменяется. |
| AC-68 | Keep-all/pins/leases/version graph/CAS prune/waiting_storage | BCCA:retention4/storage2 | S4 isolated эксплуатационный контроль; production prune не выполнялся. |
| AC-69 | Epoch/token effect checks; logout не отзывает approved job | BCCA:automation3/backend/principals3 | S4 actual revoke/expiry/reset boundaries. |
| AC-70 | Durable UsageAttempt/admission/unknown budget/no hidden retry | BCCA:attempts4/budget4 | Старые calls до ledger не реконструируются; S3 runtime limits. |
| AC-71 | FullSHA/date/separate IDs/history/partial scan no-delete | BCCA:hash3/fault14 | S5 fresh real copies/move/hash pass. |
| AC-72 | Missing keys/wait/skipped/cancelled_partial/actual counters | BCCA:resume/backend/activity10/reports; UI35 live12%->35%/pause47%/cancel51% | S2 reconnect/waits; S5 actual collection queue итог. |
| AC-73 | Principal idempotency/immutable proposals/plans/ETag/frozen deps | BCCA:principals3/API6/review/quality | S2 repeat/double action. |
| AC-74 | SSE retained IDs/resync; optional outbox/HMAC/durable retry | BCCA:activity10/webhooks10; UI35 actions/connected; PERF60 two SSE p95<1с | S2 reconnect/retention edge; webhook runtime условен по R4 и сейчас не настроен. |
| AC-75 | Durable lyric IDs/nullable/BOM/offset/occurrences/metadata | BCCA:lyrics_document6/slots15 | S2 edit/import. |
| AC-76 | One server automatic gate/reasons/fresh deps/auth/locks | BCCA:quality23 | S6 pilot обязателен; `pilot_not_validated` не обходится UI/token. |
| AC-77 | Model/PCM/temp/result budgets и isolated watchdog | BCCA:inference_budget14; CI actual model | S3 resident/long input/OOM profile. |
| AC-78 | Provider DNS/redirect guards/no playlist URL decode/text-only/CSV escape | BCCA:providers/media/backend/reports/webhooks | S4 fixed inference/proxy profile. |
| AC-79 | ID3v2.3/v2.4/native slots/unknown data, unsupported RO | BCCA:media53/slots15 | S5/S7 corpus/gapless. |
| AC-80 | Explicit checkpoint migration1->2/failure marker/legacy write barriers | BCCA:migrations9 | S4 actual migration/restore, еще не выполнены. |
| AC-81 | Frozen sidecar owners/group CAS/journals/immutable export manifest | BCCA:sidecars12/canonical1 | S2 plan/partial UI; multi-file atomic не заявлен. |
| AC-82 | HTTPS origin/Secure/CSRF/no forwarded-trust fallback | BCCA:backend/settings; historical proxy | S4 current HTTPS/direct/spoof protocol. |
| AC-83 | Legacy/owner/provider origins/known restrictions through derivatives | BCCA:policy9/artwork7/canonical1 | S2/S5 visible provenance; S4 backup/export. |
| AC-84 | Original timeline/CUE bounds/stable draft/undo/batch per-file CAS | BCCA:navigation3/CUE/album_batch9; UI35 seek/dirty/error/CUE1->3 | S2 batch conflict/undo; S6 musical timing. |

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
| CHG-015 | YouTube evidence/search/owner link без media downloads | BCCA:youtube23; S5. |
| CHG-016 | Dedicated metrics/JSON stdout/action codes | BCCA:observability12; S4 ingestion. |
| CHG-017 | Grafana47 panels/Prometheus/VictoriaLogs | S4 live scrape/panels/logs, не только import. |
| CHG-018 | Basic player transport/seek/rate/volume/keyboard/CUE | UI35 seek/rate/mute/lyrics/CUE прошли; S2 keyboard/focus. |
| CHG-019 | Previous/next/server filtered order/CUE | BCCA:navigation3; UI35 filtered30->31->32, cross-page paused/playing, dirty/error/CUE прошли. |
| CHG-020 | Persistent bottom dock/mobile | UI35 390px dock над navigation; S2 touch target fix. |
| CHG-021 | PWA manifest/install/update/public-only cache | PWA7 и UI35 first install/explicit update/public shell прошли; S7 actual Android install. |
| CHG-022 | Animations/reduced motion | S2 actions/focus. |
| CHG-023 | View/filter/track/scroll persistence/dirty guard | UI35 F5 сохраняет track32/search/view и mobile scroll469.714px; PWA update сохраняет «Обработка». |
| CHG-024 | Прежний дизайн доработан, обе темы/mobile | UI35 dark/light/390/768/1024/1440 прошли; S2 touch targets. |
| CHG-025 | Непонятная плашка копии удалена; source safety сохранена | S2 current bundle. |
| CHG-026 | [Источники файлов](file-sources.md) проработаны | Только проектирование; cloud/device adapters не реализовывать. |
| CHG-027 | Completed/cancelled/error различаются текстом/иконкой/цветом | UI35 completed green и cancelled blue подтверждены, counters/state честные. |
| CHG-028 | Percent/counts/honest active-window ETA | BCCA:activity10; UI35 real audit12% ETA2мин ->35% ETA47с, pause ETApaused; S3 long profile. |
| CHG-029 | Current actions/SSE/resync/bounded log view | UI35 two active filenames/actions/SSE; PERF60 delivery p95<1с; S2 reconnect, S3 long profile. |
| CHG-030 | Сохраняемый реестр + полный текущий audit | Финальный CHECK после результатов S1..S7. |
| CHG-031 | Heavy build/test/demo на build-01/operation lock/изоляция | BCCA green; ноутбук source/browser only. |
| CHG-032 | Завершить согласованный v1 и уведомить | Открыто S1..S7; «ТЗ выполнено» пока преждевременно. |

## Явная граница объема

Cloud/S3/Google/Yandex/Mail.ru/device file-source adapters остаются проектированием CHG-026. OIDC, GPU/remote и коммерческие/keyed профили принимаются при явной настройке; они не включаются ради отметки в таблице. Webhook delivery/n8n условны по R4 раздела16; core API/SSE обязательны. AC-60 требует переносимый экспорт и сохранность исходных плейлистов; общий внешний import/Spotify/Apple Music не превращены в core v1.

Отсутствие музыкального эталона или телефона не разрешает автоматически принять соответствующие AC. Одновременно не требуется гарантированно распознать неизвестную запись или восстановить любой поврежденный файл: нужен честный результат, сохранность данных и доступный ручной путь. Полное уведомление возможно только после результатов применимых S1..S7.
