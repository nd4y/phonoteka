import { useEffect, useState } from 'react';
import { api, messageFor } from './api';

type Token = { id: string; name: string; scopes: string[]; expires: number; revoked: number; last_used: number | null };
export type TokenScope = 'library:read' | 'jobs:create' | 'files:write' | 'publish:write';
export const TOKEN_SCOPES: ReadonlyArray<{ value: TokenScope; label: string }> = [
  { value: 'library:read', label: 'Читать коллекцию и медиа' },
  { value: 'jobs:create', label: 'Создавать и управлять задачами' },
  { value: 'files:write', label: 'Готовить предложения, записывать и откатывать Work' },
  { value: 'publish:write', label: 'Публиковать и экспортировать Ready, сообщать о доставке' },
];

export function TokenScopePicker({ scopes, onChange }: { scopes: TokenScope[]; onChange: (scopes: TokenScope[]) => void }) {
  return <fieldset><legend>Разрешения API-токена</legend>{TOKEN_SCOPES.map(option =>
    <label key={option.value}><input type="checkbox" checked={scopes.includes(option.value)} onChange={event =>
      onChange(event.target.checked ? [...scopes.filter(scope => scope !== option.value), option.value] : scopes.filter(scope => scope !== option.value))
    }/> {option.label} <small className="muted">{option.value}</small></label>
  )}</fieldset>;
}

export function AutomationPanel() {
  const [tokens, setTokens] = useState<Token[]>([]), [name, setName] = useState('n8n');
  const [scopes, setScopes] = useState<TokenScope[]>(['library:read', 'jobs:create']);
  const [secret, setSecret] = useState(''), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  async function load() {
    try { setTokens((await api<{ items: Token[] }>('/tokens')).items); }
    catch (error) { setError(messageFor(error)); }
  }
  useEffect(() => { void load(); }, []);
  async function create() {
    if (!scopes.length) return;
    setBusy(true); setError('');
    try {
      const result = await api<{ token: string }>('/tokens', { method: 'POST', body: JSON.stringify({ name, days: 30, scopes }) });
      setSecret(result.token); await load();
    } catch (error) { setError(messageFor(error)); }
    finally { setBusy(false); }
  }
  async function revoke(id: string) {
    setBusy(true);
    try { await api('/tokens/' + encodeURIComponent(id), { method: 'DELETE' }); await load(); }
    catch (error) { setError(messageFor(error)); }
    finally { setBusy(false); }
  }
  return <section className="panel settings-panel settings-wide">
    <h2>Автоматизация и API</h2>
    <p className="muted small">Токен на 30 дней для n8n или собственного скрипта. Выберите только нужные разрешения. Отзыв блокирует новые эффекты заданий токена.</p>
    {error && <p role="alert" className="warning-text">{error}</p>}
    <label>Название интеграции<input value={name} maxLength={80} onChange={event => setName(event.target.value)}/></label>
    <TokenScopePicker scopes={scopes} onChange={setScopes}/>
    <p className="muted small">Создание задач само по себе не разрешает запись файлов или публикацию. Для таких задач нужны оба разрешения: создание задач и соответствующая запись. Личную проверку Poweramp подтверждает владелец в браузере.</p>
    <button className="button secondary" disabled={busy || !name.trim() || !scopes.length} onClick={() => void create()}>Создать API-токен</button>
    {secret && <div className="notice info"><div><p>Сохраните токен сейчас: повторно он не отображается.</p>
      <input aria-label="Новый API-токен" type="password" value={secret} readOnly/>
      <div className="button-row"><button className="button secondary" onClick={() => void navigator.clipboard.writeText(secret).catch(() => setError('Не удалось скопировать. Выделите поле и скопируйте вручную.'))}>Скопировать</button>
        <button className="button subtle" onClick={() => setSecret('')}>Скрыть</button></div>
    </div></div>}
    <div>{tokens.map(token => <div className="file-facts" key={token.id}><div>
      <dt>{token.name}<small className="muted"> · {token.scopes.join(', ')}</small></dt>
      <dd>{token.revoked ? 'Отозван' : token.expires * 1000 < Date.now() ? 'Истек' : `До ${new Date(token.expires * 1000).toLocaleDateString('ru')}`}</dd>
    </div>{!token.revoked && <button className="button subtle" disabled={busy} onClick={() => void revoke(token.id)}>Отозвать {token.name}</button>}</div>)}</div>
    <p className="muted small">Передавайте токен в заголовке Authorization: Bearer. Создание задания требует Idempotency-Key. Пароль владельца интеграции не нужен.</p>
  </section>;
}
