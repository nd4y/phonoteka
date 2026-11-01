# Серверная проверка автоматического принятия

ASR-13 / AC-76 реализованы модулем `backend/phonoteka/automatic_quality.py`. Один gate возвращает `passed`, `blocked` или `needs_review`, машинные `reason_codes`, версионированный профиль, ограниченное evidence и SHA-256 всех зависимостей. Проверки продолжаются после первого отказа: отсутствие пилота не скрывает ошибочные слова, unaligned строки или stale locks.

## Фактическое состояние

Автоприменение по умолчанию выключено. Production `PROFILE.enabled=false`, `PILOT_REGISTRY={}`; результат содержит `automatic_apply_disabled` и `pilot_not_validated`. Музыкальный пилот не выполнялся этим изменением и не объявляется успешным по синтетическим тестам. Пороговые значения являются консервативным черновиком, не измеренной точностью.

Профиль и реестр пилотов только читаются из версии приложения. Ни HTTP, ни настройки БД не принимают `verified=true`, `pilot_passed` или подмену provenance. Для включения требуется отдельный проверенный музыкальный отчет, привязанный к hash профиля и контракту, затем версионированный выпуск с реестром. В этот реестр не добавлены фиктивные реальные треки. В тестах внутренние process-only overrides вызывают будущую положительную ветку, они недоступны приложению/API и не являются пилотом.

## Что проверяется

- Фактическая FileRevision, текущий последний proposal и его содержимое, записываемый формат, актуальный decode audit, отсутствие критических конфликтов, write capability, grant/epoch/token expiry/scopes.
- Ручные field locks и отдельные locks слов/времен durable LyricsDocument. Ручные решения имеют приоритет; нет обхода через выбор другого источника.
- Измененные поля должны побайтно/по нормализованному значению соответствовать сохраненному серверному candidate и его response/value hashes. API ручного предложения не может подделать эти поля; измененное вручную значение само не становится автоматически проверенным.
- Сохраняются ограничения использования исходных/вложенных provider references. Проверяются действующие embed/cache/derived_analysis permissions и версии source priorities. Неиспользованный ответ не создает право на автоматическую запись.
- Для меняющихся тегов требуется конкретный MusicBrainz recording, совпадающее независимое fingerprint evidence и явное решение владельца о версии исполнения. Конкретный релиз берется из свежей complete/subset_complete сверки с совпадением recording и позиции. Явный recording-only режим не разрешает добавлять/изменять альбомные поля. Обложка относится к конкретному принятому релизу.
- Для автоматических времен слова заранее приняты человеком, буквально совпадают по содержательным occurrences с canonical embedded document и предложением. Статус applied/accepted в LyricsDocument сам по себе не считается человеческой проверкой.
- Язык поддержан профилем и подтвержден владельцем. Alignment привязан к document ID/words revision и тем же occurrences; у 100% содержательных строк есть надежные времена, монотонность и попадание в длительность. Проверяются отдельные line/token likelihood, число неуверенных слов, скорость, длина строки и большие необъясненные разрывы. Одного coverage/среднего score недостаточно.
- Независимая ASR связана с завершенной серверной transcribe/align job той же FileRevision/remote ID. Эти серверные режимы сначала вызывают unprompted transcriber. Сопоставление учитывает повторяющиеся слова и порядок occurrences; существенное расхождение дает `independent_asr_contradiction`. Успешный forced alignment чужих слов не доказывает их правильность.

## API и исполнение

`GET /api/tracks/{id}/automatic-quality?proposal_id=...` показывает проверку сохраненного предложения. `GET /api/automatic-quality/profile` возвращает readonly профиль. `AutomaticQualityPanel` встроен в AnalysisPanel: причины, число надежных строк и snapshot dependencies. Он не предлагает включить автозапись без пилота.

`PUT /api/tracks/{id}/automatic-quality/identity` принимает только owner session + CSRF + FileRevision If-Match + base_version, существующие server candidate IDs, явную версию исполнения, release/recording_only, подтвержденный язык и текстовое основание. Это ручное решение владельца, не provider verification и не подтверждение пилота. Произвольные verified/provenance поля запрещены схемой. После изменения зависимого анализа решение становится stale.

Обычный `POST /api/jobs` с `kind=apply` по умолчанию сохраняет `acceptance_mode=manual` и прежние проверки. Только явный `params.acceptance_mode=automatic` вызывает gate при enqueue. Сервер сам сохраняет profile/dependency/proposal/apply-payload hashes в immutable job item; клиент не передает этот snapshot.

Перед staging и еще раз в authorize непосредственно перед файловым эффектом worker сверяет gate и frozen hashes. Изменение FileRevision, proposal/latest proposal, model/analysis, accepted words/review, field/line locks, provider policy, source priorities, выбранного релиза, профиля или grant требует нового принятия. Профиль по пути не понижается. История успешного автоматического эффекта получает server-generated `acceptance_mode=automatic` и gate snapshot; ручной режим получает `automatic_acceptance=false`.

Префикс `/api/v1` поддерживается общим middleware. Analysis-only token не запускает apply. Write token не получает право менять recording decisions или профиль и также проходит общий gate; наличие write scope не заменяет пилот и evidence.

## Проверка

`tests/test_automatic_quality.py` содержит негативные API/worker случаи: default-off/пустой пилот, попытки verified/provenance flags, чужие слова, unaligned/null, низкий token score, неподдержанный язык, stale words, неподтвержденный remote ASR, malformed alignment, смена policy/profile/locks/latest proposal/source priorities/grant/FileRevision до эффекта, ручной flow и analysis-only token. Test-only положительная ветка проверяет реальный enqueue frozen snapshot и последующую CAS-проверку, не записывая настоящее аудио.

Локально выполняются только AST parse и `git diff --check`. Фактический pytest/frontend/browser прогон нужен на build-01. Музыкальный пилот и проверка телефона остаются отдельной приемкой.
