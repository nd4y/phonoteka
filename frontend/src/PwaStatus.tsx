import { useState } from 'react';
import { Download, Info, RefreshCw, WifiOff, X } from 'lucide-react';
import { activatePwaUpdate, checkPwaUpdate, dismissPwaError, promptPwaInstall, usePwa } from './pwa';
import './PwaStatus.css';

interface PwaStatusProps {
  beforeUpdate?: () => boolean | Promise<boolean>;
  onBeforeReload?: () => void;
  showInstall?: boolean;
}

export default function PwaStatus({ beforeUpdate, onBeforeReload, showInstall = true }: PwaStatusProps) {
  const pwa = usePwa();
  const [help, setHelp] = useState(false);
  const [checking, setChecking] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const apply = async () => {
    if (confirming || pwa.updating) return;
    setConfirming(true);
    try {
      if (!beforeUpdate || await beforeUpdate()) await activatePwaUpdate(onBeforeReload);
    } finally { setConfirming(false); }
  };
  const install = async () => { if (!await promptPwaInstall()) setHelp(true); };
  const check = async () => { setChecking(true); try { await checkPwaUpdate(); } finally { setChecking(false); } };
  const installVisible = showInstall && !pwa.installed && pwa.supported;
  if (!installVisible && !pwa.offline && !pwa.updateReady && !pwa.error && !help) return null;

  return <section className="pwa-status" aria-label="Режим приложения">
    {pwa.offline && <div className="pwa-banner offline" role="status"><WifiOff size={18} /><div><strong>Нет подключения</strong><p>{pwa.offlineReady ? 'Сохранённый интерфейс доступен. ' : ''}Каталог, аудио и обработка требуют подключения к серверу.</p></div></div>}
    {pwa.updateReady && <div className="pwa-banner update" role="status"><RefreshCw size={18} className={pwa.updating ? 'spin' : ''} /><div><strong>Доступно обновление</strong><p>Установите, когда закончите текущие правки.</p></div><button className="button secondary" onClick={() => { void apply(); }} disabled={pwa.updating || confirming || pwa.offline}>{pwa.updating ? 'Обновляем…' : 'Обновить приложение'}</button></div>}
    {pwa.error && <div className="pwa-banner failure" role="status"><Info size={18} /><div><p>{pwa.error}</p><button className="text-button" onClick={() => { void check(); }} disabled={checking || pwa.offline}>{checking ? 'Проверяем…' : 'Проверить обновления'}</button></div><button className="icon-button" aria-label="Скрыть сообщение о режиме приложения" onClick={dismissPwaError}><X size={17} /></button></div>}
    {installVisible && <div className="pwa-install"><Download size={15} /><span>Фонотека в отдельном окне</span><button className="text-button" onClick={() => { void install(); }}>Установить приложение</button></div>}
    {help && <div className="pwa-banner help" role="status"><Info size={18} /><div><strong>Установка из браузера</strong><p>В Chrome или Edge откройте меню и выберите «Установить приложение». На iPhone или iPad: Safari → «Поделиться» → «На экран Домой». Музыка останется на сервере и будет доступна при подключении к нему.</p></div><button className="icon-button" aria-label="Скрыть подсказку по установке" onClick={() => setHelp(false)}><X size={17} /></button></div>}
  </section>;
}
