import { useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import { ApiError, messageFor } from './api';
import { needsJobAuthorization } from './jobs';
import type { Job, JobAuthorization } from './types';

const scopeLabels: Record<string, string> = {
  'jobs:create': 'Создание и выполнение этой задачи',
  'files:write': 'Запись и откат изменений в рабочей коллекции',
  'publish:write': 'Публикация и экспорт готовой коллекции',
};
const reasons: Record<string, string> = {
  authorization_revoked: 'Разрешение, выданное этой задаче, отозвано.',
  authorization_expired: 'Срок разрешения этой задачи истек.',
  authorization_epoch_changed: 'Авторизация изменилась после запуска задачи.',
  authorization_scope_denied: 'Для оставшихся операций не хватает разрешений.',
  authorization_grant_invalid: 'Предыдущее разрешение больше не действует.',
};
export function reauthorizationMessage(error: unknown) {
  const messages: Record<string, string> = {
    authorization_required: 'Для продолжения подтвердите оставшиеся операции в карточке задачи.',
    authorization_version_conflict: 'Условия подтверждения изменились. Обновите очередь и выберите разрешения снова.',
    invalid_scopes: 'Разрешения не совпадают с условиями задачи. Обновите очередь.',
    job_not_waiting_authorization: 'Состояние задачи изменилось. Обновите очередь перед следующим действием.',
  };
  return error instanceof ApiError && error.code && messages[error.code] || messageFor(error);
}

function validAuthorization(value: JobAuthorization | undefined) {
  if (!value || !Number.isSafeInteger(value.version) || value.version < 0 || typeof value.reauthorize_allowed !== 'boolean') return false;
  const required = value.required_scopes, allowed = value.allowed_scopes;
  return Array.isArray(required) && Array.isArray(allowed) && required.includes('jobs:create') && required.length > 0
    && required.length <= 3 && new Set(required).size === required.length && new Set(allowed).size === allowed.length
    && required.every(scope => Object.hasOwn(scopeLabels, scope)) && required.length === allowed.length
    && required.every(scope => allowed.includes(scope));
}

export function reauthorizationRequest(job: Job, scopes: string[]) {
  const authorization = job.authorization;
  if (!needsJobAuthorization(job) || !validAuthorization(authorization) || !authorization?.reauthorize_allowed || job.cancel_requested
    || new Set(scopes).size !== scopes.length || scopes.length !== authorization.required_scopes.length
    || !authorization.required_scopes.every(scope => scopes.includes(scope))) return null;
  return { method: 'POST', body: JSON.stringify({ scopes: [...authorization.required_scopes], authorization_version: authorization.version }) };
}

export function JobAuthorizationForm({ job, scopes, pending, onScopes, onGrant }: {
  job: Job; scopes: string[]; pending: boolean; onScopes: (scopes: string[]) => void; onGrant: (scopes: string[]) => void;
}) {
  const authorization = job.authorization;
  const valid = validAuthorization(authorization);
  const enabled = valid && !!authorization?.reauthorize_allowed && !pending && !job.cancel_requested;
  const request = reauthorizationRequest(job, scopes);
  return <form className="job-authorization" onSubmit={event => { event.preventDefault(); if (enabled && request) onGrant(scopes); }}>
    <h4><ShieldCheck size={18} />Подтвердите оставшиеся операции</h4>
    <p>{reasons[authorization?.reason || ''] || 'Для продолжения нужно актуальное разрешение владельца.'}</p>
    <p className="muted small">Идентификатор задачи и выполненные попытки сохраняются. Выберите необходимые разрешения явно.</p>
    {!valid ? <p className="muted small" role="status">Обновите очередь, чтобы получить актуальные условия подтверждения.</p>
      : <><fieldset disabled={!enabled}><legend>Разрешения для этой задачи</legend>{authorization!.allowed_scopes.map(scope =>
        <label key={scope}><input type="checkbox" checked={scopes.includes(scope)} onChange={event => onScopes(event.target.checked ? [...scopes.filter(item => item !== scope), scope] : scopes.filter(item => item !== scope))}/><span>{scopeLabels[scope]}</span></label>)}</fieldset>
        {!authorization!.reauthorize_allowed && <p className="muted small" role="status">Начатые операции завершаются безопасно. Подтверждение станет доступно после их завершения.</p>}
        <button className="button secondary" type="submit" disabled={!enabled || !request}>{pending ? 'Подтверждаем...' : 'Разрешить оставшиеся операции'}</button></>}
  </form>;
}

export function JobAuthorizationPanel({ job, pending, onGrant }: { job: Job; pending: boolean; onGrant: (scopes: string[]) => void }) {
  const [scopes, setScopes] = useState<string[]>([]);
  return <JobAuthorizationForm job={job} scopes={scopes} pending={pending} onScopes={setScopes} onGrant={onGrant}/>;
}
