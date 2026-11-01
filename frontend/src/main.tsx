import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles.css';
import './motion.css';
import './accessibility.css';
import { registerPwa } from './pwa';
registerPwa();
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
