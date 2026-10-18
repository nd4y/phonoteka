# Сверка реализации с ТЗ

Дата среза: 18 октября 2026 года. Основание: [ТЗ v1.4](specification.md), все действующие CHG из [реестра](requirements-register.md), исходный код и тесты. База Git `80182c5` плюс незакоммиченная разработка. Это аудит доказательств и оставшегося объема, а не акт полной приемки. Последующие проверки дописываются в реестр отдельным CHECK; исторические статусы не переписываются молча.

В этом аудите код и тесты прочитаны, тяжелые проверки не запускались на ноутбуке. Предыдущий CHECK-003 подтверждает сборку на build-01, 175 backend- и 15 frontend-тестов для снимка `fc05883a422cfd45d29c3bc1b71676552b6e7e4640317380525a4c4517b68c20`. Эти результаты не распространяются автоматически на последующие изменения или все сценарии AC. Состояние production, провайдеров и коллекции нужно сверять живыми отчетами заново.

Обозначения:

- **Код/тесты**: основной контракт присутствует и покрыт указанными тестами; это не означает, что весь комплексный AC уже принят на целевой инфраструктуре.
- **Частично**: часть обязательного поведения отсутствует или не имеет полного доказательства.
- **Не реализовано**: соответствующего рабочего маршрута при чтении исходников не обнаружено.
- **В работе**: текущая доработка, для которой еще нужна итоговая сборка и функциональная проверка.
- **Нужна внешняя проверка**: физическое устройство, музыкальная разметка, живая инфраструктура или разрешенный внешний API.
- **Условно не применяется**: только явно выключенный опциональный профиль с основанием; это не отменяет обязательный core.

Не рассчитывается общий процент готовности из количества зеленых unit-тестов. Один AC часто включает несколько отказов, реальную инфраструктуру и приемку результата на устройстве.

Таблица обновлена после CHECK-005: новые маршруты отделены от еще не выполненной приемки. Результат 305-pass относится только к указанному диагностическому срезу; последние исправления и новые тесты требуют чистого образа.

## Критерии AC-01..84

Ссылки на тесты указывают на существующие сценарии, а не на новый запуск в рамках этого чтения.

| AC | Статус на срезе | Доказательство и оставшаяся проверка |
| --- | --- | --- |
| AC-01 | Код/тесты; нужна проверка поставки | [Compose](../compose.yaml), [container smoke](../tests/container_smoke.py), CHECK-003. Для новой версии нужны CI, образ, app/worker restart и сохранность состояния на st1. |
| AC-02 | Код/тесты | [media](../backend/phonoteka/media.py): `_validate`, `_file`, `writer_guard`; [tests](../tests/test_media.py): `test_original_and_links_are_never_writable`. Реальные mounts/ACL целевого NAS проверяются отдельно. |
| AC-03 | Код/тесты; нужна текущая сверка | [worker](../backend/phonoteka/worker.py): `scan`, [intake](../backend/phonoteka/intake.py), [backend tests](../tests/test_backend.py): bad audio, WEBM, extensionless, missing root. Нужны свежие counts/ошибки фактической коллекции. |
| AC-04 | Код/тесты | `test_analysis_is_read_only_select_requires_current_revision` в [backend tests](../tests/test_backend.py), анализ не пишет музыку. Массовый dry-run требует нынешней before/after hash-сверки коллекции. |
| AC-05 | Код/тесты | [media tests](../tests/test_media.py): roundtrip, picture slots, foreign/unknown ID3 frames, version preservation. Разные реальные пользовательские профили остаются частью приемочного корпуса. |
| AC-06 | Код/тесты | [providers tests](../tests/test_providers.py): variant/duration conflicts; предложения не применяются автоматически. Музыкальный live/remix/cover-набор проверяется отдельно. |
| AC-07 | Код/тесты | [media tests](../tests/test_media.py): независимое чтение Mutagen, packet identity, cover и rollback. Устройство проверяется по AC-17. |
| AC-08 | Код/тесты | [media tests](../tests/test_media.py): LRC equal timestamps, offset, invalid/partial; [frontend tests](../frontend/src/lyrics.test.ts). |
| AC-09 | Код/тесты | `test_analysis_plain_lyrics_are_an_untimed_draft`, `test_lrc_partial_is_not_exported_as_complete`. Наличие текста не выдает синхронность. |
| AC-10 | Код/тесты; неполная fault matrix | [media tests](../tests/test_media.py): recovery sync after rename, unknown hash; [backend tests](../tests/test_backend.py): inflight/heartbeat failure. Нужны все точки crash/fsync/DB commit на целевой FS. |
| AC-11 | Код/тесты; неполная fault matrix | Atomic space reservation, insufficient space, missing root, invalid audio в [tests](../tests/test_media.py). Реальные RO/remount и повторные disk failures отдельно. |
| AC-12 | Код/тесты | [providers](../backend/phonoteka/providers.py), [provider resume tests](../tests/test_provider_resume.py): backoff, durable wait, skips done. Живая доступность сервисов не гарантируется fixtures. |
| AC-13 | Код/тесты | [app](../backend/phonoteka/app.py): job idempotency, frozen apply payload; `test_idempotency_proposal_cas_and_snapshot`. API retry не должен подменять выбранный пакет. |
| AC-14 | Код/тесты | `test_mutation_conflict_leaves_external_change`, SHA preconditions. Защита от независимого одновременного писателя требует реальных ACL/maintenance SAFE-11. |
| AC-15 | Код/тесты | `test_rollback_conflict_does_not_clobber_new_changes`, `test_manual_roundtrip_recovery_publish_and_rollback`. Удаленный backup blob должен отображаться недоступным, не как успешный rollback. |
| AC-16 | Код/тесты; нужна эксплуатационная приемка | [maintenance](../backend/phonoteka/maintenance.py), [maintenance tests](../tests/test_maintenance.py): manifest/hash, credentials revoked, recovery mode. Нужен повторяемый отдельный stack и сверка всех зависимостей. |
| AC-17 | Нужна проверка на устройстве | Не менее 10 MP3 и 10 FLAC, офлайн Poweramp, обложка/строки/seek. В репозитории нет протокола физического выполнения этого набора. |
| AC-18 | Нужна текущая сверка | [collection reports](../backend/phonoteka/reports.py) дают counts, но завершение задания не доказывает распознавание. Требуются свежий смешанный пакет и итоги по файлам/источникам/ошибкам. |
| AC-19 | В работе; нужна браузерная приемка | [App](../frontend/src/App.tsx), [AudioPlayer](../frontend/src/AudioPlayer.tsx), CSS. Нужны 390/768/1024/1440, keyboard, reduced motion, mobile player и защита черновика. |
| AC-20 | Не проверено по полному профилю | Пагинация есть, но 10 000 записей, FILE_WORKERS=4, 2 vCPU/4 GiB, p95 и bounded memory не подтверждены требуемым нагрузочным протоколом. |
| AC-21 | Код/диагностические тесты; чистый образ повторить | [reports](../backend/phonoteka/reports.py) дает actual per-kind file outcomes/codes, no-match/ожидания источников, audit, publication exclusions/profile, provenance и CSV formula protection в одном DB snapshot. [tests](../tests/test_report_outcomes.py) вошли в 305-pass CHECK-005. |
| AC-22 | Код/тесты | Disabled providers и ручной путь работают независимо от внешнего API. [provider tests](../tests/test_providers.py), [catalog tests](../tests/test_catalogs.py). Нужна полная UI-проверка offline-core. |
| AC-23 | Код/тесты; неполное измерение | [worker](../backend/phonoteka/worker.py): bounded async queue + executor; [backend tests](../tests/test_backend.py) подтверждают независимые очереди. Нужна трасса >=2 файлов с thread IDs и перекрытием await. |
| AC-24 | Код/тесты; неполная конкурентная матрица | Single scheduler/storage writer guards; тест второго worker. Нужна конкуренция apply/apply/rollback одного файла и внешнего stack. |
| AC-25 | Не проверено по полному профилю | Отдельные ASR/ordinary lanes проверены, но отсутствует требуемая 15/30-минутная нагрузка, API p95 и event-loop lag по разделу 15. |
| AC-26 | Код/тесты; нужны нагрузочные измерения | [providers](../backend/phonoteka/providers.py): semaphore, per-provider spacing, budget/cooldown. Tests проверяют отказ/429; медленный источник под полной очередью отдельно. |
| AC-27 | Частично; нагрузка не проверена | `bounded_items` ограничивает active tasks, но задания/scan materialize до 10 000 items. Нет протокола RAM/fd/backpressure при этом объеме. |
| AC-28 | Код/тесты; нужна crash-проверка | `test_mutation_scheduler_failure_drains_inflight_write_before_exit`, `test_final_heartbeat_failure_cannot_release_guards_before_executor_drains`. Физический restart с двумя writes отдельно. |
| AC-29 | Свободный маршрут реализован; адаптация CHG-013 | [recognition](../backend/phonoteka/recognition.py), [tests](../tests/test_recognition.py): ShazamIO fingerprint. ACRCloud/AudD не реализованы; CHG-013 запрещает создавать/включать ключевые адаптеры без настройки владельцем. Не выдавать их за работающие. |
| AC-30 | Частично | Разные/сомнительные кандидаты и отсутствие automatic apply есть. Полноценная сверка конфликтов независимых участков микса/ASR/lyrics на реальной музыке не принята. |
| AC-31 | Код/тесты | Разделенные ProviderError, durable request reservations, retry waits. [providers tests](../tests/test_providers.py), [recognition tests](../tests/test_recognition.py). Missing keys не превращаются в no-match. |
| AC-32 | Код/тесты; нужно tag-only доказательство | [recognition](../backend/phonoteka/recognition.py): fingerprint-based cache. Полный сценарий изменения только тегов с доказанным отсутствием нового вызова должен быть отдельно в приемке. |
| AC-33 | Код/тесты | `test_real_fingerprint_contains_no_audio_or_private_metadata`, transport allowlist, audio hash. Реальный outgoing audit должен сохранять только разрешенные сведения. |
| AC-34 | Не выполнено: музыкальный пилот | 30 песен RU/EN с заранее зафиксированной разметкой и независимым набором настройки отсутствуют. Один реальный пример с 50% строк из [recognition.md](recognition.md) не достигает заданного критерия. |
| AC-35 | Код/тесты; музыкальный негативный набор не принят | [alignment](../backend/phonoteka/alignment.py), [forced alignment](../backend/phonoteka/forced_alignment.py), [tests](../tests/test_alignment.py). No automatic acceptance; нужны >=10 музыкальных негативных случаев. |
| AC-36 | Код/тесты; браузерная приемка открыта | [TimingReview](../frontend/src/TimingReview.tsx) различает offset/drift, показывает исходное/предложенное время, принимает строку или общий сдвиг только в draft; слова/undo сохраняются. [timing tests](../frontend/src/timing.test.ts), CHECK-005. Музыкальные времена требуют AC-34. |
| AC-37 | Код/тесты для включенного профиля | CPU transcribe/align работает в исходной временной шкале, VAD выключен, отрезки bounded. [inference](../backend/phonoteka/inference.py), [smoke](../tests/inference_smoke.py). VAD/отделение вокала не включены; не заявлять их проверенными. |
| AC-38 | Код/тесты | ASR draft и nullable alignment, `automatic_acceptance=False`; частичные строки не экспортируются как полный текст. Музыкальная точность отдельно. |
| AC-39 | Код/тесты; неполный OOM-профиль | [backend tests](../tests/test_backend.py): slow inference не блокирует ordinary lane; durable inference job. Реальный OOM/restart и API p95 под лимитом требуют протокола. |
| AC-40 | Частично; только CPU-профиль | Реальная модель включена в CI inference smoke. CPU 2/RAM 3 GiB определен в Compose; RAM peak/RTF/музыкальный контроль не зафиксированы полностью. GPU/remote профили не включены и не приняты. |
| AC-41 | Код/тесты; параллельный bootstrap не проверен | [security.bootstrap](../backend/phonoteka/security.py) создает единственного owner из защищенного файла, без сетевой регистрации. Двойной первый startup и multi-device сессии проверить отдельно. |
| AC-42 | Код/тесты | Cookie/CSRF/origin, audio/art/export auth в [backend tests](../tests/test_backend.py). Static shell публичен, приватные API/медиа защищены. Нужно сохранить при добавлении PWA. |
| AC-43 | Код/тесты | [automation](../backend/phonoteka/automation.py): scoped bearer/check_job; [tests](../tests/test_automation.py). Background authorize проверяет token перед эффектом. Auto-apply профиль отсутствует. |
| AC-44 | Код/диагностические тесты; UI повторить | POST auth/logout-all с owner+CSRF отзывает все sessions, не отменяя одобренные jobs/API tokens. [backend tests](../tests/test_backend.py) проверяют 2 сессии/CSRF/библиотеку; отдельные token revoke и локальный reset сохранены. CHECK-005. |
| AC-45 | Условно не применяется | OIDC явно опционален и не включен. Core owner-login обязателен. При включении модуля весь subject/fail-closed/recovery AC становится обязательным. |
| AC-46 | Код/контрактные тесты; внешняя проверка | [catalogs](../backend/phonoteka/catalogs.py), [tests](../tests/test_catalogs.py): отключение, release identity. Живой ключ Discogs/включенный TheAudioDB не подтверждены; по умолчанию disabled. |
| AC-47 | Код/диагностические тесты; UI повторить | [field-policy](field-policy.md): per-field locks/CAS и source priorities, старые proposals/worker gates. [tests](../tests/test_field_policy.py) и независимые words/timing locks [LyricsDocument](lyrics-document.md). Согласованная поставка app+worker обязательна. |
| AC-48 | Код/диагностические тесты; расширенная приемка открыта | [provider-policy](provider-policy.md): fetch/cache/embed/export/derived_analysis, TTL и provenance, transient preview, inheritance без ручного снятия запрета, backup/history cleanup. [tests](../tests/test_provider_policy.py), CHECK-005. Неизвестное право не названо лицензией. |
| AC-49 | Код/тесты | Core независим от Spotify/Apple Music/коммерческих lyrics/n8n/OIDC. Не подключенные сервисы не считать успешными интеграциями. Условия обязательных источников остаются AC-48. |
| AC-50 | Код/тесты; неполная decoder матрица | Decode audit, invalid catalog items, Ready blocks failed audit. [backend](../tests/test_backend.py), [media](../tests/test_media.py). Многопоточные контейнеры/неожиданный хвост всех профилей требуют приемочного корпуса. |
| AC-51 | Код/диагностические тесты; UI повторить | Signal diagnostics показывают silence/peaks/possible clipping как warnings, не меняют decode verdict и не доказывают lossless. Реальные WAV cases в [tests](../tests/test_media.py) вошли в CHECK-005. |
| AC-52 | Код/диагностические тесты; A/B UI повторить | [comparison](../backend/phonoteka/comparison.py), [ComparePanel](../frontend/src/ComparePanel.tsx): параметры/metadata/lyrics, exact/audio-packet/ID candidate groups. A/B переносит позицию только при доказанном совпадении, live/cover/remix остаются кандидатами. [tests](../tests/test_comparison.py). |
| AC-53 | Код/диагностические тесты; UI повторить | Versioned preferred/publish-both/defer и superseded overlap CAS в [comparison](../backend/phonoteka/comparison.py); [exports](../backend/phonoteka/exports.py) сохраняет явные aliases без удаления/переноса личных полей. CHECK-005. |
| AC-54 | Код/диагностические тесты; UI/live API повторить | [release-review](release-review.md): exact MB release, discs/positions, complete/subset/unknown, owner subset CAS. [album-batch](album-batch.md): frozen per-file proposals/исключения/locks, atomic save без autoapply. Fixtures вошли в CHECK-005; внешний каталог не гарантируется. |
| AC-55 | Код/тесты; устройство отдельно | [loudness tests](../tests/test_loudness.py), [album tests](../tests/test_album_loudness.py): joint gate, explicit selected group, gain roundtrip/rollback в семи профилях. Физический Poweramp Gain входит в AC-63. |
| AC-56 | Код/диагностические тесты; повторить в чистом образе | [lyrics-review](../backend/phonoteka/lyrics_review.py): accepted FileRevision, per-line listening, lyrics_required/instrumental/relaxed с исключениями; publication gate и frozen exact policy/review до эффекта. Tests вошли в CHECK-005; новая metadata/review инвалидирует queued publish. |
| AC-57 | Код/тесты; нужны реальные ACL | Disjoint paths, no links, publication atomic copy. Сервисная учетная запись sync и ее RO только Ready проверяются в NAS/клиенте отдельно. |
| AC-58 | Код/диагностические тесты; новая fault matrix открыта | Atomic audio/per-object sidecar journals и immutable export generation audio+cover+LRC, manifest только подтвержденного состава; частичность не названа атомарностью. CHECK-005 и [sidecar contract](publication-sidecars.md), новые [fault tests](../tests/test_fault_acceptance.py) еще требуют build-01. |
| AC-59 | Код/диагностические тесты; UI повторить | [publication](../backend/phonoteka/publication.py): независимый rollback_ready по exact destination SHA, intents/recovery, внешнее изменение не затирается, work не откатывается вместе с Ready. [tests](../tests/test_publication.py), CHECK-005. |
| AC-60 | Код/диагностические тесты; пакетная приемка открыта | [exports](../backend/phonoteka/exports.py): relative UTF-8 M3U8, проверенные targets, aliases preferred copies; [publication_paths](../backend/phonoteka/publication_paths.py): NFC/case/platform collisions. Исходные плейлисты не изменяются; обязательного импорта внешних плейлистов PUB-08 не задает. |
| AC-61 | Контракт реализован; внешняя проверка | [automation.md](automation.md) описывает one-way client и receipts. Фонотека не управляет Android напрямую. Реальные настройки FolderSync/RO-учетки и отсутствие обратной записи проверить отдельно. |
| AC-62 | Код/диагностические тесты; клиент отдельно | Immutable independent audio+sidecar files, authenticated Range downloads и leases сохраняют конкретное поколение; partial checksum не выдается за verification. [exports](../tests/test_exports.py), [retention](../tests/test_retention.py), CHECK-005. Физические resume/ENOSPC клиента не приняты. |
| AC-63 | Нужна проверка на устройстве | Ready -> Android -> offline Poweramp, playlist, Track/Album Gain и revision/hash. Owner receipt не заменяет фактическую проверку. |
| AC-64 | Код/тесты | [exports tests](../tests/test_exports.py): export stale по track SHA, receipts bound export. Нужна реальная повторная доставка измененных тегов с прежним именем. |
| AC-65 | Код/тесты; инфраструктура частично | Root identity, alias/link rejection, shared writer guard, no missing on partial scan. Реальные два stack/вложенные roots/SMB rights/lost mount проверить на изолированной FS. |
| AC-66 | Код/тесты; неполная fault matrix | Общий space reservation, fsync recovery и create/no-replace в [media tests](../tests/test_media.py). Полная комбинация backup/fsync/rename/DB failures при двух writes требует дополнительной приемки. |
| AC-67 | Код/тесты; неполная restore матрица | Restore recovery mode + work/Ready SHA acknowledge; [maintenance tests](../tests/test_maintenance.py). Missing rollback blob и более новые work/Ready отдельно, не восстанавливать старую БД поверх live коллекции. |
| AC-68 | Код/диагностические тесты; эксплуатационная приемка открыта | [retention](retention.md): keep-all default, policy/pins/version graph, explicit CLI plan+hash+revalidation, journals, leases и download flock. waiting_storage preflight retries; post-journal ENOSPC только recovery. [tests](../tests/test_retention.py), [storage tests](../tests/test_storage_wait.py). Production prune не выполнялся. |
| AC-69 | Код/тесты; недостающие границы | [automation tests](../tests/test_automation.py), revoked authorization в [media tests](../tests/test_media.py). Нужен revoke/expiry ровно между очередью и каждым новым эффектом; logout не должен отзывать одобренную job. |
| AC-70 | Код/тесты добавлены; новый прогон требуется | [provider-attempts](provider-attempts.md): opaque ledger/reservation до HTTP, sent/known/unknown, no refund/no hidden unknown retry. 20 concurrent files/remaining budget2/restart/timeout cases подготовлены; они новее CHECK-005 305-pass. |
| AC-71 | Код/тесты; нужны move/mtime cases | Scan full SHA, внешняя подмена получает новый ID/history не сливается. Две копии/перемещение/те же size+mtime и partial scan требуют полного набора проверок. |
| AC-72 | Код/тесты; живые счетчики проверить | [provider resume tests](../tests/test_provider_resume.py), cancel_partial/paused. Missing keys/manual path видны. Нужна свежая сверка counts на фактических ожиданиях коллекции. |
| AC-73 | Код/диагностические тесты; расширение gate повторить | Principal-scoped durable idempotency, frozen proposals, ETag/CAS; frozen accepted publication review/profile до audit/effect. Automatic gate дополнительно замораживает profile/dependencies; его новые tests еще требуют чистого build-01. |
| AC-74 | Core и optional outbox в коде/тестах; live webhook не включен | Durable SSE IDs/retention/resync, one selected activity SSE + fallback. [webhooks](webhooks.md): bounded transactional outbox/reservation, signed delivery/retry/dead UI; по R4 конфигурация условная и выключена. После recovery итог сверяется GET/SSE, одно terminal notification на принятую попытку. Внешних отправок не было. |
| AC-75 | Код/диагностические тесты; последние UI fixes повторить | [LyricsDocument](lyrics-document.md): persistent IDs/ordinal/nullable/offset/import, separate words/timing revisions+locks, structured proposals и alignment invalidation. [tests](../tests/test_lyrics_document.py), CHECK-005 305-pass. Исправления stale UI flags после locks еще новее этого результата. |
| AC-76 | Исходный снимок: частично; позднее реализован код, ожидает проверки | После исходного снимка добавлен единый server gate с reason codes, readonly versioned profile, frozen dependencies enqueue/effect, полными per-line проверками и field/line locks: [контракт](automatic-quality.md), [negative/CAS tests](../tests/test_automatic_quality.py). Production auto выключен, реестр музыкального пилота пуст: `pilot_not_validated`. Код и synthetic checks не подтверждают музыкальное качество или пройденный build-01 прогон. |
| AC-77 | Код/диагностические тесты; lifecycle fixes/real model открыты | [ASR profile](asr-resource-profile.md): один supervised resident model, RAM/PCM/temp/result/deadline и fail+unload без autoretry; contract v3 включает profile в identity. Настоящие child-process tests вошли в CHECK-005, peer review выявил send/shutdown races, исправления проверяются следующим срезом. GPU выключен. |
| AC-78 | Код/тесты | HTTP allowlist/redirect checks, FFmpeg protocol+format allowlist, text rendering, CSV formula escaping. [provider tests](../tests/test_providers.py), [recognition tests](../tests/test_recognition.py), [backend tests](../tests/test_backend.py). Полную security matrix повторить после новых адаптеров. |
| AC-79 | Код/тесты | v2.3/v2.4 preservation, unknown frames, multiple APIC and packet side data в [media tests](../tests/test_media.py). Реальный gapless playback на устройстве отдельно. |
| AC-80 | Код/диагностические тесты; production migration не выполнялась | [schema migrations](schema-migrations.md): explicit offline1->2 с sanitized verified checkpoint+blob refs/pins, legacy-write barriers, transactional ledger и failure marker. 10 real SQLite/fault cases вошли в CHECK-005. Существующая production1 еще не менялась. |
| AC-81 | Исходный снимок: частично; позднее реализован код, ожидает проверки | Плейлист исходного снимка строился после проверки Ready targets. Позднее добавлены opt-in shared cover/LRC sidecars, frozen owner-plan, single-object journals, partial/retry и immutable all-object export: [контракт и ограничения](publication-sidecars.md), [failure tests](../tests/test_publication_sidecars.py). Нельзя заявить сценарий пройденным до полного build-01 и браузерного прогона. |
| AC-82 | Код/тесты; нужна live proxy проверка | Secure cookie/origin/TrustedHost; forwarded headers не принимаются как auth. Новую версию проверить через internal HTTPS proxy и прямой NAS HTTP без понижения cookie policy. |
| AC-83 | Код/диагностические тесты; текущая поставка открыта | [provider-policy](provider-policy.md) отличает preexisting/owner_supplied/provider_acquired с inherited restrictions, hashes/provenance сохраняются при ручных edits. Raw denied draft не переносится в canonical LyricsDocument. Unknown происхождение явно сохраняется; CHECK-005. |
| AC-84 | Код/диагностические тесты; browser/physical acceptance открыта | Общий media timeline/CUE bounds и dirty navigation guard, persistent view/scroll, LyricsDocumentEditor/undo и [album-batch](album-batch.md) all-or-none per-file CAS/exclusions. Требуются реальные browser/seek tests и музыкальное сравнение позиции; unit pass их не заменяет. |

## Поправки CHG-001..031

Эта таблица не дублирует исходный текст требований. Полные критерии остаются в реестре.

| CHG | Статус на срезе | Доказательство / оставшееся |
| --- | --- | --- |
| CHG-001 | Частично; нужна проверка устройства | Теги/обложка/SYLT/LRC присутствуют, AC-17/34/63 не завершены. |
| CHG-002 | Код/исторические проверки | RO original + отдельная work. Полную текущую копию/инвентарь сверить заново. |
| CHG-003 | Код/поставка | Самостоятельный Compose/worker/API, без зависимости n8n. |
| CHG-004 | Код/тесты; нагрузка открыта | Ограниченные async/executor lanes; AC-20/23/25/27/28 требуют полного протокола. |
| CHG-005 | Частично | Реальные CPU ASR и DTW; музыкальный пилот и дополнительные lyrics state/gate функции открыты. |
| CHG-006 | Частично | ShazamIO/MB/LRCLIB/CAA и отключенные catalogs; приоритеты/usage contract открыты AC-47/48/83. |
| CHG-007 | Код/тесты | Один owner, sessions/CSRF, без регистрации. Logout-all остается AC-44. |
| CHG-008 | Код/макеты | Material 3 Expressive, обе темы. Новый UI нужен в браузерной приемке. |
| CHG-009 | Документировано | Архивы v1.1..v1.4, review и 84 AC сохранены. Наличие требований не равно реализации. |
| CHG-010 | Историческая поставка; новая версия не принята | GitHub + st1 + internal proxy есть. Для текущих dirty изменений еще нужны CI/release/контрольный deploy. |
| CHG-011 | Код/тесты | CUE encodings/files/INDEX, PCM-verified extraction, read-only images. Новый player navigation проверить по CUE. |
| CHG-012 | Частично | Format capability map/read-only fallback/normalization derivatives есть; универсальная идентификация/ремонт повреждений не обещаются. Весь реальный corpus не принят. |
| CHG-013 | Соблюдено | Free-first path; ключевые адаптеры не создаются/не активируются без настройки. Это уточняет старый обязательный ACRCloud/AudD runtime. |
| CHG-014 | Код/историческое разрешение; результат живой | Separate flags, provider limits/cache/retry. Текущее число распознанных определяет отчет, не total completed attempts. |
| CHG-015 | Код/тесты | [YouTube](youtube.md), provenance/candidates/owner confirmation, без скачивания. Связь с конкретным upload остается доказательством источника/владельца. |
| CHG-016 | Код/тесты | [observability](observability.md), dedicated metrics auth, JSON stdout. Live scrape/log ingestion нужны после обновления. |
| CHG-017 | Код/dashboard; live verify отдельно | Grafana JSON47 panels существует; импорт не подтверждает свежие метрики/логи во всех панелях. |
| CHG-018 | Код/тесты; браузерная проверка | AudioPlayer transport/seek/volume/rate/retry/keyboard/CUE. Browser codec limits остаются явными. |
| CHG-019 | В работе | Backend neighbors и три теста есть; full filtered order/dirty failure/CUE/paused-playing проверить в новом UI. |
| CHG-020 | В работе | Постоянный нижний dock есть в dirty source и предыдущем demo; current mobile/desktop acceptance не завершена. |
| CHG-021 | В работе | Нужны manifest/SW/install/update/offline и доказательство исключения API/auth/private audio из caches. |
| CHG-022 | В работе | Нужны согласованные animations/reduced motion и проверки действий без scroll jumps. |
| CHG-023 | В работе | Нужны restore page/editor/filter/track/scroll после async data и F5/update, draft protection. |
| CHG-024 | В работе | Сохранить основу; обе темы, mobile, contrast/focus/dialog/popover и реальное управление. |
| CHG-025 | В работе | Плашку тестовой копии убрать; storage safety остается. |
| CHG-026 | Проработка завершена; реализация отложена явно | [file-sources.md](file-sources.md), CHECK-002. Облачные/device adapters не разрешены этой проработкой. |
| CHG-027 | В работе | Отмена не должна получать green success, текст/иконка/цвет согласованы. |
| CHG-028 | В работе | Число процентов, counts и честная ETA по активной скорости; unknown/provider wait/paused отдельно. |
| CHG-029 | В работе | Authenticated bounded live events/current actions/reconnect, без принудительной прокрутки читателя. |
| CHG-030 | Документировано; итоговая сверка открыта | requirements-register + этот аудит; новых CHECK недостаточно для закрытия отсутствующих функций. |
| CHG-031 | Проверено в CHECK-003; для новых изменений повторить | build-01 isolated builder/tests/demo/tunnel. Не трогать соседние сборки; ноутбук только source/browser. |

## Обязательные следующие работы

1. Завершить текущие player/PWA/jobs/reload/animations, проверить реальными браузерными действиями, собрать на build-01 и поставить проверенную версию через GitHub/st1.
2. Реализовать остающиеся обязательные продуктовые маршруты: field locks/provenance/source permissions, complete lyrics document/review/gate, duplicates/preferred copy, album/release/subset, publication profiles/immutable audio exports/Ready rollback и sidecar plan. Добавить соответствующие осмысленные failure tests. Отключение функции по умолчанию не закрывает ее обязательный AC.
3. Учитывать явную условность webhook delivery/n8n по этапу R4 раздела16 ТЗ: при отсутствии конфигурации/прав соответствующий подпункт AC-74 не включен. Core SSE/API остаются обязательными. OIDC и будущие Spotify/Apple Music также явно опциональны; новых разрешений для них не предполагается.
4. Выполнить воспроизводимую эксплуатационную приемку: нагрузка10k, API/event-loop p95, память/fd, concurrency/rate budgets, crash/fsync/restore и несовпадающие файловые ревизии на изолированном стенде. Базовые unit-тесты не заменяют ее.
5. Подготовить заранее размеченный музыкальный набор AC-34/35, отдельно от настройки модели; зафиксировать coverage/median/p95/отказы по песням/языкам. Выполнить физическую офлайн-проверку Android/Poweramp и one-way доставки с версиями/hashes.
6. Перед уведомлением «ТЗ выполнено» добавить финальный CHECK со ссылкой на каждый применимый AC/CHG, commit/image/report и фактический результат. Непроверенный музыкальный пилот или телефон не отмечать автоматически пройденными.

Новые AC-51 измерения основаны на официальной [документации FFmpeg astats/silencedetect](https://ffmpeg.org/ffmpeg-filters.html#astats-1). Sample peaks и extrema не являются доказательством слышимого clipping; длинная тишина может быть авторской. Диагностика не меняет музыку и не устанавливает lossless-происхождение.

## Последующие изменения разработки 18 октября

После исходного среза добавлен [field-policy](field-policy.md): per-field owner locks для всех native tags/MB/ReplayGain, cover SHA и полного lrc; версии/CAS и file SHA; пропуск locked candidate fields; запрет старых proposals при enqueue и worker; блокировка policy-изменений во время ожидающей/активной записи. Появились отдельные TrackPolicyPanel/SourcePriorityPanel и семь сценариев в [test_field_policy.py](../tests/test_field_policy.py), включая реальную FLAC-обложку. **AC-47 теперь в работе, реализация добавлена, удаленная проверка и приемка UI еще требуются.** Это не закрывает раздельные слова/время LYR-03, preferred-copy DUP, provider permissions AC-48 или общий ASR gate AC-76. Приоритеты не включают автоматическую запись. Новые app и worker должны поставляться согласованно; старый worker не обеспечивает новые locks.

Добавлена [сверка выбранного release](release-review.md): worker job `release_audit`, конкретный MusicBrainz release ID и ограниченный tracklist, сравнение дисков/треков/ID/названий/исполнителей/длительностей, конфликты/missing/extra/unknown, versioned owner subset и JSON/CSV. ReleaseReviewPanel позволяет проверить состав перед запуском и перейти к проблемному файлу. **AC-54 теперь в работе: исходники и семь fixture/backend сценариев добавлены, build-01 и браузерная приемка еще требуются.** Группировка по albumtag не выдается за полноту издания. Качество аудио, правильность тегов, публикация отдельного файла и выбранный состав независимы; новых групповых тегов/обложек эта доработка не создает. Права источника/TTL применяются к сохраненному эталону и backup/cleanup.

Следующим этапом добавлен отдельный [album batch](album-batch.md) для ALB-04: явный состав, preview для каждого файла, owner tags или server-bound release fields/cover candidate, индивидуальные исключения, locks/usage/provenance, CAS файла и последнего proposal. Сохранение proposals атомарно в БД и идемпотентно; manifest содержит immutable proposal IDs. Apply jobs/музыкальные записи не запускаются автоматически. Девять backend-сценариев подготовлены, включая отказ второй вставки и запрет manual-copy обхода прав. **ALB-04 в работе до удаленного прогона и браузерной приемки**, без заявления о завершении всего ТЗ.
