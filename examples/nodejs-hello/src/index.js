const express = require('express');
const app = express();
const port = parseInt(process.env.PORT || '8080', 10);

app.get('/', (_req, res) => res.json({ service: 'hello', message: 'built with pack via rdx_app' }));
app.get('/health', (_req, res) => res.send('ok'));
app.get('/ready', (_req, res) => res.send('ok'));

app.listen(port, '0.0.0.0', () => console.log(`hello listening on :${port}`));
