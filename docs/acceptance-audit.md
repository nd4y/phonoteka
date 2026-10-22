# Сверка ТЗ v1.4: реализация и приемка

## Актуализация 22 октября 2026 года

На st1 уже установлен `bb98d3e42534e59583237fb07237cf2ba38a8a94` / 0.7.0-alpha / schema 2: matched GitHub 39737711667, 610 backend / 170 frontend tests, postflight 42 target / 19 origin / 63 Grafana queries. Повторная read-only проверка контейнеров healthy / restart0 / OOMfalse выполнена 22 октября. Scan/analyze2012 и missing7 завершены; прежние формулировки «нужны rescan и resume2012» ниже относятся к историческому срезу 20 октября и не являются инструкцией повторить задания.

Разрешенный музыкальный ASR-пилот завершен технически 2/2, но выровнял 0/66 строк. Полное ТЗ не принято. Финальные browser/Poweramp/музыкальные проверки остаются открытыми; успешный серверный gate их не заменяет. Найденные дефекты, patch candidate и условия выпуска сведены в [production-readiness](production-readiness.md). Историческая матрица ниже сохранена для прослеживаемости и читается вместе с CHECK-031..039 реестра.

## Текущий срез от 20 октября 2026 года

Обновление после исходного среза ниже: на st1 установлен `3733afd24d5bc91270cf1d2b9cb83cc27417395f`, [GitHub 39413670083](https://github.com/nd4y/phonoteka/actions/runs/39413670083) - четыре успешных задания, 589 backend тестов/60 subtests и 167 frontend тестов. Все три контейнера healthy, restart0/OOMfalse. Изменены только три image ID; перед обновлением создан свежий приватный checkpoint `image-only-3733afd-2c1e141c390849e885a256b68178f9aa`. Схема2 и приватные пути сохранены. После обновления прошли 42 target, 19 origin и 17 isolated restore проверок; метрики/stdout и все63 запросов Grafana без ошибок. Протоколы находятся в `output/music-library/release-07-final-3733afd-20261020T113516Z` вне репозитория. CHECK-030 фиксирует этап поставки.

ASR больше не ожидает ресурсов: полный build-01 профиль прошел19 ASR,7 combined и15 ordinary критериев за480с обычной нагрузки; CI повторил19/7/15. Original build-01 controller остается global FAIL из-за появления двух посторонних контейнеров; существовавшие до запуска контейнеры не изменились, свои удалены. Отдельное решение допускает этот функциональный результат с сохранением полного diff и исходного FAIL. Фактическое сравнение B4/3733 подтвердило одинаковые backend/dependencies/model, compiled frontend и параметры исполнения; собственные четыре проверочных контейнера удалены, foreign diff0. Результаты B4 NFR и остальных серверных профилей перенесены только на этом основании.

Полное ТЗ по-прежнему не принято. После поставки еще нужны штатный rescan, audit50 и возобновление той же задачи2012, затем missing7 при предусмотренных условиях. Браузер в текущем этапе по указанию владельца не используется. Ручные Poweramp и независимый музыкальный пилот отложены до конца; успешные серверные проверки их не заменяют. Следующие абзацы сохраняют исторический срез до этой поставки, а не текущее состояние st1.

Кандидат - `b4f07d0b99bfb93aa19c7629d1c7f2308ed9b4cb`, [GitHub 39324362666](https://github.com/nd4y/phonoteka/actions/runs/39324362666) завершился успешно: 571 backend и 167 frontend тестов. Проверка исходников связывает этот commit с app image `1532aac0433e` и inference image `252a2b4`. Демо на build-01 доступно по [доверенному HTTPS](https://phonoteka-build.st1.int.nd4y.icu/) без SSH-туннеля. Основание приемки - 84 AC и CHG-001..037 [реестра](requirements-register.md).

**ТЗ еще не принято полностью.** Успешные браузерные проверки PWA, плеера, CUE, защиты черновика и мобильной верстки записаны в [частичном протоколе](../../output/music-library/release-07-final-b4f07d0-20261019T210500Z/browser-continuation-20261020.json) и CHECK-016. Проверка обновления после восстановления демо выполнена в новой вкладке; прежняя вкладка потеряла debugger. Не утверждается, что ее состояние было восстановлено. Серверные live ETA/pause/resume/cancel проверены отдельно в CHECK-019; проверка потери сеанса в браузере остается незавершенной.

ASR480 завершился тайм-аутом на actual ASR; обычная нагрузка в этом запуске прошла15 критериев за480с, но это не успех всего профиля. Повтор ASR ждет освобождения ресурсов. Исходные NFR30 с14/15 сохранены как FAIL. Новый полный NFR512 run20261020T091116Z-acf883754b14 прошел все15 критериев за1800,001с с10k записей/4реальными worker/2SSE: пик cgroup285396992 при512МиБ, raw growth47146052/noncache53574315 при64МиБ. Exact cleanup и host OOM0 подтверждены. Raw controller остается FAIL из-за4 независимых изменений RelayWeave; отдельная typed assessment принимает функциональные результаты/невмешательство после проверки полного diff и37 отрицательных тестов классификатора. Подробности CHECK-025. VM и код приложения не менялись.

После кандидата b4f07d0 GitHub700a8da прошел581backend/167frontend и real ASR smoke; runtime source tree одинаков, actual images equivalence пока не проверена. В CHECK-026 добавлен отдельный воспроизводимый CI-профиль19ASR+7совместных проверок с исходными лимитами и проверкой installed source; первый запуск этого нового CI пока ожидается. Его результат не заменяет build-01/музыкальный пилот/телефон.

На st1 только чтением подтверждены три healthy контейнера `754e29e4626227a2992e30bf42df2b7b591f0c4f`; кандидат `b4f07d0` не установлен. Это подтверждает [наблюдение production](../../output/music-library/release-07-final-b4f07d0-20261019T210500Z/live-containers-20261020-continuation.json), а не приемку нового deployment. Poweramp на телефоне и независимый музыкальный пилот отложены владельцем до конца; внешние источники файлов остаются проектированием. Ни один из этих пунктов не считается пройденным из-за успешного CI или health.

Серверное продолжение по CHG-037 не использует браузер и не перезагружает build-01. CHECK-017 подтверждает защищенные метрики/JSON stdout и 63 корректных запроса Grafana действующей версии, а также штатные queued/pause/resume/cancel/SSE/logout на синтетическом демо без worker. CHECK-019 подтверждает фактический прогресс, ETA, текущие действия, паузу с drain, возобновление и частичную отмену активного аудита. ASR и устранение причины NFR FAIL остаются незавершенными.

### Уже полученные серверные доказательства кандидата b4f07d0

Это отдельные фактически выполненные профили. Их результат не переносится на оставшиеся этапы или production автоматически. [Индекс отчетов с SHA](../../output/music-library/release-07-final-b4f07d0-20261019T210500Z/server-evidence-index-20261020.json) сохраняет исходные результаты, границы и открытые проверки.

| Профиль | Подтверждено | Ограничение |
| --- | --- | --- |
| Очередь 10000 файлов | 17 критериев прошли: четыре реальных файловых worker, backpressure, прогресс, отмена с безопасным завершением текущих файлов, ограниченные ресурсы и HTTP. | Не заменяет 30-минутный NFR или полное распознавание production-коллекции. |
| Пакет для Poweramp | 20 generated MP3/FLAC, 5 критериев: запись/повторное чтение тегов, обложки и текста, неизменность аудиопакетов, SHA и архив. | На телефоне пока не проверен. Использован фактически завершенный отчет, прежний преждевременно опубликованный статус сохранен отдельно. |
| Native NAS | Проверка записи и восстановления на целевых файловых системах прошла; очистка собственного контейнера подтверждена. | Только отдельные generated private файлы; это не deployment или восстановление production-состояния. |
| Аудит 50 файлов | Точный кандидат проверил 50 файлов с полными SHA/stat: 3 корректных аудио, 47 ожидаемых отказов некорректного содержимого, неожиданных ошибок нет. | Не означает, что все 50 файлов являются аудио или распознаны музыкальными сервисами. |
| Публикация | Все 31 функциональный критерий и очистка прошли. Изменения четырех чужих lifecycle объяснены отдельным проверенным отчетом. | Исходный aggregate gate остается FAIL; отдельная оценка не переписывает его и сама не разрешает deployment. |
| HTTP security | Все 39 функциональных критериев и очистка прошли. Изменения четырех чужих lifecycle объяснены отдельно. | Исходный aggregate gate остается FAIL; ASR, NFR и браузерное поведение этим не принимаются. |
| Live progress/ETA/cancel | Аудит 42/42 без ошибок, монотонный процент, положительный ETA и текущие действия; отдельный аудит paused/drained/resumed/cancelled_partial 2/42, завершение через 0,873 с по серверным событиям. Worker удален, файлы и защищенные Ready/CUE сохранены. | Исходные контрольные сценарии ошибочно ожидали другие terminal-state и остались FAIL. Отдельная оценка проверяет сохраненные измерения и события; это серверная приемка, без браузера, NFR или ASR. |

## Историческая матрица от 19 октября

Следующие доказательства и таблицы относятся к прежнему кандидату `3f37b909989876fa919e94d4d52403d675d10dc9` и CHG-001..032. Они сохранены для происхождения проверок. Указанные ниже слова «текущий», номера версий и незавершенные этапы описывают тот исторический срез; они не заменяют состояние кандидата и production, указанное выше. Итоговая сверка всех 84 AC будет дополнена после оставшихся фактических проверок.

В историческом срезе исправления frontend прошли build-01 и GitHub gate; реальные HTTP security checks image3f прошли. Ordinary API/Worker NFR30 прошел на предыдущем GitHub image35b с неизменным backend source tree. Весь v1 тогда не был принят. Подтвержденный на тот момент production оставался 0.6.0-alpha. Browser touch/dirty/PWA протокол, inference resource profile и целевая поставка еще не были завершены.

Матрица различает наличие кода, полученное доказательство и оставшуюся проверку. Процент готовности из числа unit-тестов не вычисляется. История прежних отказов и исправлений сохраняется в CHECK реестра и Git; эта текущая сверка не переписывает требования и не меняет current-release.

Новый фактический отказ после этого среза: actual job из10000 WAV на3f не завершил отмену за30с при active0. Исправление bounded admission/compact job controls и пять новых регрессий готовятся к новому полному gate; прежний30-минутный NFR больше не является доказательством неизменного финального backend. Две CSS правки и устранение устаревшего import warning после Undo входят в тот же кандидат. См. CHECK-010. Владелец отложил S6/S7 на конец (CHG-033); это не PASS и не открывает automatic gate.

## Доказательства

Локальные ссылки на отчеты ведут за пределы Git в каталог оператора. Оригиналы build-01 лежат в `/home/av/build/phonoteka/reports/`. Тяжелые проверки выполнялись там, не на ноутбуке.

| ID | Подтверждено | Граница |
| --- | --- | --- |
| B8AE | [8ae45e0-backend.xml](../../output/music-library/release-07/8ae45e0-backend.xml): **409 passed, 0 failed/error/skipped**, JUnit215.327с; source snapshot `8ae45e09...`, run `20261018T215459Z-408895`, **58 frontend tests**, TS/Vite/runtime прошли. | После runtime build потерян descriptor controller. Backend возобновлен на проверенных том же source snapshot/image; controller backend227.4с не подменяет JUnit215.327с. Это не заявление о непрерывном запуске одного controller. Предыдущие BCCA/B79 отчеты сохранены. |
| CI3F | [GitHub dispatch39169455513](https://github.com/nd4y/phonoteka/actions/runs/39169455513): **completed/success**, revision3f37b90; app/inference прошли, включая настоящую ASR/alignment/negative/dedup и real app/worker container smoke. | [App](../../output/music-library/release-07/loaded-image.json) `6b823bb81451` и [inference](../../output/music-library/release-07/loaded-inference.json) `3770a9fe990f` имеют одинаковую OCI revision, archive/artifact SHA проверены. App загружен и на build-01. Загрузка/подготовленный plan не равны deployment. |
| CI35 | [Предыдущий GitHub39166290627](https://github.com/nd4y/phonoteka/actions/runs/39166290627): completed/success, revision35b020f. | Его [app](../../output/music-library/release-07/stages/35b020f/loaded-image.json) `f93e121e2a60` использован в PERF30; [inference](../../output/music-library/release-07/stages/35b020f/loaded-inference.json) `1c326634342b`. Старые image records сохранены отдельно от текущих. |
| UI35 | [Протокол действий CUA](../../output/music-library/release-07/browser-functional-evidence.json), [responsive](../../output/music-library/release-07/responsive-player.json), [PWA](../../output/music-library/release-07/pwa-browser-evidence.json): фильтр/соседи/dirty/error/seek/lyrics/CUE; live ETA/pause/cancel; 390/768/1024/1440px, dark/light; F5/update сохраняют контекст. | Обычный owner login, только generated fixtures. PWA server-unavailable проверен остановкой своего review app, не физическим отключением сети. Android install/Poweramp не проверены. Это протокол взаимодействий, а не вывод о функциональности из скриншота. |
| NAS35 | [Storage acceptance](../../output/music-library/release-07/storage-acceptance-latest.json): реальная volume2/volume3, shared writer guard, reopen tags/cover/lyrics, packet preservation, committed verified backup; официальный snapshot/restore при реально более новых work/Ready удержал recovery gate и сохранил файлы. | Отдельные generated private files; production mounts отсутствовали, музыка production не копировалась, identity production services сохранены. Это проверка целевой FS и recovery, не миграция production DB и не подтверждение sync-account ACL. |
| OLD | [J945 diagnostic](../../output/music-library/release-07/945959-context-diagnostic/diagnostic.xml):385/7; [J227](../../output/music-library/release-07/227c701-backend.xml):402/2; CI39158310594:399/5. | Исторические failed candidates; причины и fixes в CHECK-007/[разборе](../../output/music-library/release-07/ci-39158310594-failures.md). Не выданы за принятые образы. |
| FF6 | [Официальный Ubuntu24.04/FFmpeg6 эксперимент](../../output/music-library/release-07/ffmpeg6-warning-evidence.json) установил точную mono-layout note. | Остальные/fatal diagnostics не подавлены; соответствующие regressions теперь прошли BCCA. |
| PERF60 | [60-секундный release smoke](../../output/music-library/release-07/performance-release-60s.json) на GitHub app image35b020f:10k строк,120HTTP,2 сессии,4 настоящих Worker/FFmpeg,2vCPU/1GiB; list/search/status p95 **85.82/90.00/333.47мс**, lag p95 **26.48мс**, SSE p95 **531.12/519.73мс**; все measured checks прошли. | 220 updates/15 bounded batches, max4 concurrent files, max progress gap2.09с; resources учитывают child processes. Старый failed SSE smoke сохранен. Этот успешный короткий smoke не заменяет >=15/30 минут и1000HTTP; inference/providers в нем не участвуют. |
| PERF30 | [30-минутный ordinary профиль](../../output/music-library/release-07/performance-release-30m.json), [image/profile](../../output/music-library/release-07/performance-profile-release-30m.json): **1800.001с/1800HTTP**,10k записей,2UI+SSE,4 real Worker/FFmpeg,2vCPU/1GiB, **15/15 measured checks true**. List/search/status p95 **106.90/164.61/412.03мс**, lag p95 **43.62мс**, SSE p95 **511.96/512.65мс**. | Измерен именно app image35b/f93, не текущий3f/6b. [Git source equivalence](../../output/music-library/release-07/backend-source-equivalence.json): backend tree одинаковый `2cbd860...`, Dockerfile/requirements/pyproject/harness без diff. 5034 updates/315 batches по16 generated FLAC, max4 concurrent/max progress gap7.75с; RAM/fd/tasks/children в пределах отчетных лимитов, OOM не наблюдался. Не закрывает inference/providers или отдельную очередь10000файлов AC-27. |
| SEC3F | [Actual HTTP security](../../output/music-library/release-07/security-http-20261018T223822Z-9548d9de.json): **25/25 checks**, current app image3f/6b; два настоящих uvicorn, concurrent bootstrap, multi-session, CSRF/Origin/cross-site negatives, scopes/revoke/logout/logout-all, generated password-file reset/epoch и блокировка pending write. | network none/RO rootfs/1CPU384MiB/no host binds, generated state/media only; собственный контейнер удален, OOM false. Expiry - явно изолированная dependency clock simulation+172801с, не реальное суточное ожидание; host time и DB expiry не менялись. HTTPS/Secure/proxy и browser cache/logout остаются отдельными проверками. |
| P06 | [Исторический production GET](../../output/music-library/release-07/before-status.json):0.6.0-alpha,2153 записи, worker last_seen19:08:42Z. | Не свежий итог покрытия и не поставка0.7. Кандидат/попытка/завершенный HTTP не подтверждают правильность каждого трека. |

`B8AE: media53` в таблице означает 53 успешных testcase модуля `tests/test_media.py` в указанном JUnit. Это не 53 принятых AC. `SRC` означает чтение кода без отдельного выполненного приемочного сценария. Полный B8AE закрывает программную регрессию перечисленных модулей именно на этом срезе; каждый составной AC дополнительно сверяется с колонкой «Осталось». PERF30 содержит прямую привязку к старому image35b; source equivalence не объявляет два бинарных image одинаковыми.

## Конечные обязательные этапы v1

Это приемка уже согласованного объема, без новых функций.

| Этап | Конкретно осталось | AC/CHG |
| --- | --- | --- |
| S1. Поставочный gate | B8AE и текущий CI3F с matched app/worker/inference image pair прошли; verify archive/OCI revision сохранен. S4 должен запустить именно эту подтвержденную пару и сверить live identities. | CHG-010/031/032. Gate выполнен; production-поставка еще нет. |
| S2. Browser/PWA/наблюдаемость | UI35 подтвердил player/filter/CUE/dirty/error/seek/lyrics, mobile/themes, refresh/update, live percent/ETA/actions/pause/cancel. Touch/dirty frontend fixes прошли58 tests/current CI; итоговый browser protocol еще дополняется. Остались отдельные owner workflows locks/album review/A-B/sidecar, keyboard/focus/reduced-motion и cache/reconnect edge cases; после поставки свежие scrape/stdout/Grafana. SEC3F уже подтвердил session/token/logout/reset по actual HTTP. Android installation относится к S7. | AC-19/22/47/53/54/74/84; CHG-008/016..025/027..029. |
| S3. Полный NFR/ресурсы | На b4f07d0 полный NFR5121800с/1800HTTP/10k записей/4worker/2SSE прошел15/15, отдельная очередь10000 файлов прошла17 проверок. Generated ASR19+ordinary7 в CI3f7f4d5 функционально прошли, но полные отчеты потеряны из-за квоты Artifacts, общий workflow FAIL сохранен. Повтор build-01 ждет ресурсы; исправленный канал CI-отчетов требует проверки. | CHECK-025..027. Новые runtime sources совпадают с b4; actual images equivalence и финальная поставочная привязка еще не проверены. |
| S4. Целевая установка | NAS35 подтвердил shared guard/отдельный official restore при newer work/Ready на настоящей FS; SEC3F подтвердил bootstrap/multi-session/revoke/reset/epoch в одноразовом actual HTTP экземпляре. Остались controlled st1/internal HTTPS deploy, RO/disjoint production mounts/UID/GID/sync ACL, checkpoint/явная migration1->2/restart, недоступный backup и direct HTTP/forwarded spoof/Secure-cookie boundaries. Production DB не подменять тестовым restore. | AC-01/02/11/16/24/41/42/44/57/65/67/69/80/82. Plan подготовлен, production deploy/migration еще не выполнены. |
| S5. Фактическая коллекция | Свежий full inventory/audit и смешанный review-пакет рабочей копии: каждый файл/CUE/WEBM учтен либо имеет причину; dry-run сохраняет fullSHA; tags/art/lyrics/errors/CSV/JSON сходятся с реестром. Проверить разрешенные metadata/fingerprint ветви и варианты записи. Не принуждать no_match к success и не обходить budgets. | AC-03..07/18/21/29..33/50..56/60/72. P06 счетчики не закрывают этап. |
| S6. Музыкальный пилот | Независимые30RU/EN песен:15+15,20studio+10сложных; >=10negative случаев, заранее зафиксированный человеческий эталон. Для>=70%studio покрытие>=90% всех эталонных строк при median<=300мс/p95<=800мс; результаты/отказы по песне и языку. Непринятые строки остаются partial. | AC-30/34..38/76. [Протокол](musical-pilot.md) есть, выполненного отчета нет. `PILOT_REGISTRY={}`, automatic default off, `pilot_not_validated` сохраняются. |
| S7. Android/Poweramp | >=10MP3+10FLAC реально доставлены и проверены offline: tags/front cover/строки в начале/середине/конце/после seek, UnicodeM3U8, Track/AlbumGain; one-way RO transport, partial/resume/ENOSPC, новая revision с прежним именем и независимые device stamps. | AC-17/55/57/61..64/79. Генератор `dev/poweramp_fixtures.py` и пустой result.csv не заменяют телефон. |

На этом срезе подтвержденных отсутствующих обязательных backend-маршрутов не установлено. Это не обещание отсутствия дефектов или закрытия S1..S7. Новые отказы фиксируются относительно существующего AC, без бесконечного расширения объема. Физический power-cut NAS не объявляется выполненным и не добавляется молча как новая обязательная операция.

## Матрица AC-01..84

Все исходные номера сохранены. Названия реализации - модули `backend/phonoteka`, если не указано иное. S1 остается общей поставочной границей всех строк; ниже дополнительно указаны специфические остатки.

| AC | Реализовано в HEAD | Проверено | Осталось |
| --- | --- | --- | --- |
| AC-01 | Compose/non-root/persistent state | Историческая P06; smoke BCCA | S4 новый NAS restart. |
| AC-02 | media roots/no-links/no-original-write | B8AE:media53/fault14 | S4 mounts/ACL; S5 SHA. |
| AC-03 | worker/intake/signatures/CUE/invalid rows | B8AE:backend27/CUE20/hash3 | S5 свежий полный каталог. |
| AC-04 | Read-only analysis/dry-run preview | B8AE:backend | S5 before/after fullSHA. |
| AC-05 | Native unknown tags/multiple slot preservation | B8AE:media53/slots15 | S5 реальный corpus/кириллица. |
| AC-06 | Variant/mix conflict, no automatic acceptance | B8AE:providers11/quality23 | S5/S6 live/remix/cover. |
| AC-07 | Native write/reopen/packet hash/selected front | B8AE:media53/slots15/canonical_sidecars1 | S5 corpus, S7 device. |
| AC-08 | LRC equal times/offset/error validation | B8AE:media53/lyrics_document6; UI35 lyric click/seek5с | S2 import/редактор. |
| AC-09 | Plain/partial имеет nullable times, не synced | B8AE:backend/lyrics_document6 | S2 понятное состояние UI. |
| AC-10 | Prepared journal/before-after recovery/DB reconciliation | B8AE:fault14/media53 | Программная fault matrix пройдена; S4 поставка на целевую FS. |
| AC-11 | Aggregate space reserve/RO/missing/error | B8AE:fault14/storage_wait2 | S4 RO/lost mount target. |
| AC-12 | Bounded timeout/retry/backoff/cooldown | B8AE:providers11/resume2 | S3 поведение под нагрузкой. |
| AC-13 | Immutable proposals/idempotency per principal | B8AE:API6/principals3/backend | S2 двойное UI действие. |
| AC-14 | SHA CAS enqueue/stage/effect, external conflict | B8AE:backend/media/fault | S4 права независимых писателей. |
| AC-15 | Work/Ready rollback, backup/hash/CAS | B8AE:publication4/fault/slots; NAS35 real backup/restore | S2 UI; S4 production deploy/recovery boundaries. |
| AC-16 | Separate restore/dependencies/recovery mode/revoked credentials | B8AE:maintenance6/fault14; NAS35 separate official restore/newer media | S4 production migration/auth boundaries; missing blob. |
| AC-17 | Embedded tags/cover/SYLT/Vorbis + device package | B8AE writer; generator SRC | S7 физические20 файлов; не принято. |
| AC-18 | Actual file outcomes/reasons/sources/review | B8AE:reports2/backend | S5 mixed real package. |
| AC-19 | Responsive themes/dock/focus/motion/keyboard | Frontend gate B8AE; UI35 390/768/1024/1440px dark/light, player/jobs | S2 исправить/измерить touch targets>=44px, keyboard/focus/reduced motion. |
| AC-20 | Pagination/search/limited workers/backpressure | NFR512 b4:10k/1800HTTP/30мин/4workers,15/15; list/search/status p9539,46/90,71/208,28мс | CHECK-025: raw FAIL lifecycle сохранен, отдельная scoped assessment пройдена. Для нового CI-кандидата нужна actual image equivalence. |
| AC-21 | CSV/JSON actual results/provenance/exclusions/formula escaping | B8AE:reports2/backend | S5 реальные счетчики. |
| AC-22 | Local editor/history/rollback без providers | B8AE:backend/catalogs2 | S2 offline core UI. |
| AC-23 | Async lanes/executor/real parallel files | PERF60 threads; B8AE:fault/backend | S3 I/O+network overlap trace. |
| AC-24 | File lock/CAS; Gain lock до catalog persist | B8AE:fault14/group_gain7; NAS35 shared guard blocks second writer | S4 production storage-domain/mount binding. |
| AC-25 | API отдельно от file/inference lane; lag metrics | PERF30(35b):HTTP p95<1с/lag43.62мс/30мин resources | Ordinary профиль прошел; S3 отдельный inference overlap/resource profile. |
| AC-26 | Semaphore-before-throttle/shared budget/deadline | B8AE:providers/resume/attempts4 | S3 slow-source fairness/rate trace. |
| AC-27 | Bounded queue/tasks/executor, streaming decode/hash | Отдельная b4 queue10000:17 проверок, реальные4worker,58,623с, source60/exact cleanup; NFR512 RAM/fd/tasks bounded | Отчет final-queue-20261019T220425Z-95c25b1ef5a3.json. Не переносить на новый image без проверки actual closure/source identity. |
| AC-28 | Pause/shutdown drain in-flight, guard удержан | B8AE:fault14/backend | S4 controlled ownership handoff. |
| AC-29 | Free-first ShazamIO; recording/release разделены | B8AE:recognition6/budget4/audit3 | S5 actual music. ACR/AudD без ключей не требуются по CHG-013. |
| AC-30 | Ambiguous mix/variant/wrong text видимы, не autoaccept | B8AE:recognition/providers/quality/align | S5/S6 реальные negatives. |
| AC-31 | no_match/401/429/timeout/budget различимы | B8AE:attempts4/budget4/recognition | S3 лимиты полной очереди. |
| AC-32 | Audio-identity cache переживает tags/new batch | B8AE:recognition/budget4 | S5 реальный tag-only цикл. |
| AC-33 | Consent/fingerprint samples; metadata audio не отправляет | B8AE:recognition/audit/providers | S5 actual requests + unchangedSHA. |
| AC-34 | CPU alignment + протокол метрик | CI speech smoke; пилота нет | S6 обязательные30 песен. |
| AC-35 | Independent ASR/wrong-text/unaligned gates | B8AE:alignment3/quality23; CI smoke | S6 repeats/silence/instrumental negatives. |
| AC-36 | Timing review global shift/local drift, owner draft | B8AE:alignment; UI source | S2/S6 actual offset/drift. |
| AC-37 | Resample/crop windows -> original timeline, work unchanged | B8AE:alignment; CI speech | S6 шкала времени. Vocal separation выключен. |
| AC-38 | ASR draft/partial/unsupported language/nullable | B8AE:alignment/budget14/lyrics | S6 языки и отказы. |
| AC-39 | Independent inference lane/dedup/deadlines/isolated child | B8AE:backend/inference_budget14; CI model | S3 реальный OOM/restart/API/resource profile. |
| AC-40 | Configured CPU transcribe/align/model contract | Настоящая CI3F ASR-ветка | S3/S6 CPU ресурсы/контроль; GPU/remote условны и выключены. |
| AC-41 | Fixed owner/password file/bootstrap/sessions | B8AE:backend auth; SEC3F actual concurrent bootstrap/2live sessions | HTTP contract проверен; S4 текущая HTTPS deployment binding. |
| AC-42 | Cookie/CSRF/origin/private media, public-only SW | B8AE:backend/exports/PWA7; SEC3F CSRF/Origin negatives; UI35 install/update/public shell | S2 browser logout/cache; S4 HTTPS Secure/proxy/direct/spoof. Physical offline не проверен. |
| AC-43 | Scoped analysis/write tokens + worker/automatic gates | B8AE:automation3/quality23; SEC3F actual read/analysis/write scopes/revoke | Runtime scope contract проверен; S4 deployment binding, S6 automatic pilot. |
| AC-44 | Logout-all/token revoke/local reset/epoch | B8AE:backend/automation; SEC3F actual multi-session/logout-all/file-reset/epoch | HTTP/reset contract проверен на generated state; S2 browser cache/logout. |
| AC-45 | OIDC отсутствует/не включен; local owner есть | Config/docs | Условный optional AC; не новый core модуль. |
| AC-46 | Disabled TheAudioDB/Discogs adapters/limits/normalization | B8AE:catalogs2 | Live keyed профиль только при настройке/правах. |
| AC-47 | Field locks/priorities/manual first/CAS, words/timing locks | B8AE:field_policy7/lyrics_document6 | S2 owner workflow. |
| AC-48 | Fetch/cache/embed/export/analysis rights/TTL/transient/inheritance | B8AE:policy9/artwork7/canonical1 | S2/S5 provenance; S4 restore/export. |
| AC-49 | Core независим от commercial/n8n/OIDC/Spotify/Apple | B8AE:disabled providers/core | S2 honest disabled/not configured UI. |
| AC-50 | All-stream audit before fingerprint/Ready; bad file state | B8AE:multistream2/audit3/media | S5 actual damaged/cut corpus. |
| AC-51 | Silence/sample peak/possible clipping warnings; lossless unknown | B8AE:media53 + FF6 evidence | S5 реальные примеры; нет необоснованного удаления. |
| AC-52 | Exact/packet/candidate groups, A-B/format facts | B8AE:comparison4/reports/backend | S2 A-B; S5 разные исполнения. |
| AC-53 | Versioned preferred/both/defer, explicit aliases/no deletion | B8AE:comparison4/exports4 | S2; S7 playlist/device. |
| AC-54 | Exact MB release/discs/IDs/duration/unknown/subset; batch proposals | B8AE:release7/album_batch9 | S2; S5 allowed live release/corpus. |
| AC-55 | Track/joint Gain, immutable group/audio/release CAS | B8AE:loudness8/album5/group7 | S5 состав; S7 Poweramp Gain. |
| AC-56 | Accepted FileRevision/lyrics_required/instrumental/relaxed/frozen gate | B8AE:lyrics_review9/publication/API | S2/S5 реальный accepted/ASR-negative пакет. |
| AC-57 | Disjoint/no-hardlink/copy-to-Ready/separate sync rights | B8AE:media/backend; historical compose | S4 real ACL; S7 sync account. |
| AC-58 | Object journals/recovery/immutable generation; partial != atomic batch | B8AE:publication4/sidecars12/canonical1 | S2 partial states; S4 target update. |
| AC-59 | Frozen Ready CAS/named plan/commit/independent rollback | B8AE:publication4/API6 | S2; S4 external target conflict. |
| AC-60 | Relative UTF-8 M3U8/verified targets/aliases/NFC-case collision | B8AE:exports4/sidecars/canonical1 | S5/S7 playlist. Общий импорт внешних playlists не предписан. |
| AC-61 | One-way RO exports/receipt contract, no hidden device control | B8AE:exports; automation docs | S4 sync ACL, S7 actual client. |
| AC-62 | Immutable files/Range/leases/no unproved checksum | B8AE:exports4/retention4 | S7 partial/resume/ENOSPC клиента. |
| AC-63 | Published/transferred/verified/Poweramp states разделены | B8AE:exports; device generator | S7 offline телефон; не принято. |
| AC-64 | Receipt bound export/fileSHA; newrevision invalidates evidence | B8AE:exports4 | S7 новая доставка с прежним именем. |
| AC-65 | Shared guard/disjoint/alias/link/lost-root protection | B8AE:backend/fault14; NAS35 blocks second writer on real FS | S4 production mounts/SMB/sync ACL. |
| AC-66 | Aggregate reserve/backup/fsync/rename/DB failure reconciliation | B8AE:fault14/media53/storage2 | Программная fault matrix пройдена; не выдается за отказ питания NAS. |
| AC-67 | Restored-state recovery/newer media conflict/unavailable blob | B8AE:fault/maintenance6/migrations9; NAS35 actual newer work/Ready preserved | S4 missing backup runtime boundary; live DB не заменяется. |
| AC-68 | Keep-all/pins/leases/version graph/CAS prune/waiting_storage | B8AE:retention4/storage2 | S4 isolated эксплуатационный контроль; production prune не выполнялся. |
| AC-69 | Epoch/token effect checks; logout не отзывает approved job | B8AE:automation3/backend/principals3; SEC3F actual revoke/reset/logout effects, pendingwrite blocked | Expiry проверен actual HTTP/Worker с явно диагностическим clock fixture; production state reset не выполнялся и не требуется ради теста. |
| AC-70 | Durable UsageAttempt/admission/unknown budget/no hidden retry | B8AE:attempts4/budget4 | Старые calls до ledger не реконструируются; S3 runtime limits. |
| AC-71 | FullSHA/date/separate IDs/history/partial scan no-delete | B8AE:hash3/fault14 | S5 fresh real copies/move/hash pass. |
| AC-72 | Missing keys/wait/skipped/cancelled_partial/actual counters | B8AE:resume/backend/activity10/reports; UI35 live12%->35%/pause47%/cancel51% | S2 reconnect/waits; S5 actual collection queue итог. |
| AC-73 | Principal idempotency/immutable proposals/plans/ETag/frozen deps | B8AE:principals3/API6/review/quality | S2 repeat/double action. |
| AC-74 | SSE retained IDs/resync; optional outbox/HMAC/durable retry | B8AE:activity10/webhooks10; UI35 actions/connected; PERF60 two SSE p95<1с | S2 reconnect/retention edge; webhook runtime условен по R4 и сейчас не настроен. |
| AC-75 | Durable lyric IDs/nullable/BOM/offset/occurrences/metadata | B8AE:lyrics_document6/slots15 | S2 edit/import. |
| AC-76 | One server automatic gate/reasons/fresh deps/auth/locks | B8AE:quality23 | S6 pilot обязателен; `pilot_not_validated` не обходится UI/token. |
| AC-77 | Model/PCM/temp/result budgets и isolated watchdog | B8AE:inference_budget14; CI actual model | S3 resident/long input/OOM profile. |
| AC-78 | Provider DNS/redirect guards/no playlist URL decode/text-only/CSV escape | B8AE:providers/media/backend/reports/webhooks | S4 fixed inference/proxy profile. |
| AC-79 | ID3v2.3/v2.4/native slots/unknown data, unsupported RO | B8AE:media53/slots15 | S5/S7 corpus/gapless. |
| AC-80 | Explicit checkpoint migration1->2/failure marker/legacy write barriers | B8AE:migrations9 | S4 actual migration/restore, еще не выполнены. |
| AC-81 | Frozen sidecar owners/group CAS/journals/immutable export manifest | B8AE:sidecars12/canonical1 | S2 plan/partial UI; multi-file atomic не заявлен. |
| AC-82 | HTTPS origin/Secure/CSRF/no forwarded-trust fallback | B8AE:backend/settings; historical proxy | S4 current HTTPS/direct/spoof protocol. |
| AC-83 | Legacy/owner/provider origins/known restrictions through derivatives | B8AE:policy9/artwork7/canonical1 | S2/S5 visible provenance; S4 backup/export. |
| AC-84 | Original timeline/CUE bounds/stable draft/undo/batch per-file CAS | B8AE:navigation3/CUE/album_batch9; UI35 seek/dirty/error/CUE1->3 | S2 batch conflict/undo; S6 musical timing. |

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
| CHG-007 | Single owner/sessions/CSRF/tokens/logout-all/reset | SEC3F25checks actual HTTP прошли; S4 current HTTPS/proxy и S2 browser logout/cache. |
| CHG-008 | Material3 Expressive/темы/макеты | S2. |
| CHG-009 | Архивы ТЗ/review/84AC сохранены | Финальная сверка, требований не удаляли. |
| CHG-010 | GitHub CI3F/current matched app+inference gate прошел | S4 новая st1/internal proxy поставка еще не выполнена. |
| CHG-011 | CUE/INDEX/encodings/PCM derivatives | S2/S5. |
| CHG-012 | Explicit formats/errors/RO/normalization | S5 corpus, без обещания ремонта любой порчи. |
| CHG-013 | Free-first вместо paid adapters без ключей | Соблюдено; уточняет AC-29. |
| CHG-014 | Consent/shared budgets/retry/cache | S5 fresh coverage report. |
| CHG-015 | YouTube evidence/search/owner link без media downloads | B8AE:youtube23; S5. |
| CHG-016 | Dedicated metrics/JSON stdout/action codes | B8AE:observability12; S4 ingestion. |
| CHG-017 | Grafana47 panels/Prometheus/VictoriaLogs | S4 live scrape/panels/logs, не только import. |
| CHG-018 | Basic player transport/seek/rate/volume/keyboard/CUE | UI35 seek/rate/mute/lyrics/CUE прошли; S2 keyboard/focus. |
| CHG-019 | Previous/next/server filtered order/CUE | B8AE:navigation3; UI35 filtered30->31->32, cross-page paused/playing, dirty/error/CUE прошли. |
| CHG-020 | Persistent bottom dock/mobile | UI35 390px dock над navigation; S2 touch target fix. |
| CHG-021 | PWA manifest/install/update/public-only cache | PWA7 и UI35 first install/explicit update/public shell прошли; S7 actual Android install. |
| CHG-022 | Animations/reduced motion | S2 actions/focus. |
| CHG-023 | View/filter/track/scroll persistence/dirty guard | UI35 F5 сохраняет track32/search/view и mobile scroll469.714px; PWA update сохраняет «Обработка». |
| CHG-024 | Прежний дизайн доработан, обе темы/mobile | UI35 dark/light/390/768/1024/1440 прошли; S2 touch targets. |
| CHG-025 | Непонятная плашка копии удалена; source safety сохранена | S2 current bundle. |
| CHG-026 | [Источники файлов](file-sources.md) проработаны | Только проектирование; cloud/device adapters не реализовывать. |
| CHG-027 | Completed/cancelled/error различаются текстом/иконкой/цветом | UI35 completed green и cancelled blue подтверждены, counters/state честные. |
| CHG-028 | Percent/counts/honest active-window ETA | B8AE:activity10; UI35 real audit12% ETA2мин ->35% ETA47с, pause ETApaused; S3 long profile. |
| CHG-029 | Current actions/SSE/resync/bounded log view | UI35 two active filenames/actions/SSE; PERF60 delivery p95<1с; S2 reconnect, S3 long profile. |
| CHG-030 | Сохраняемый реестр + полный текущий audit | Финальный CHECK после результатов S1..S7. |
| CHG-031 | Heavy build/test/demo на build-01/operation lock/изоляция | B8AE/SEC3F/PERF30; ноутбук source/browser only. |
| CHG-032 | Завершить согласованный v1 и уведомить | Открыто S1..S7; «ТЗ выполнено» пока преждевременно. |

## Явная граница объема

Cloud/S3/Google/Yandex/Mail.ru/device file-source adapters остаются проектированием CHG-026. OIDC, GPU/remote и коммерческие/keyed профили принимаются при явной настройке; они не включаются ради отметки в таблице. Webhook delivery/n8n условны по R4 раздела16; core API/SSE обязательны. AC-60 требует переносимый экспорт и сохранность исходных плейлистов; общий внешний import/Spotify/Apple Music не превращены в core v1.

Отсутствие музыкального эталона или телефона не разрешает автоматически принять соответствующие AC. Одновременно не требуется гарантированно распознать неизвестную запись или восстановить любой поврежденный файл: нужен честный результат, сохранность данных и доступный ручной путь. Полное уведомление возможно только после результатов применимых S1..S7.
