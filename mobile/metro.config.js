const { getDefaultConfig } = require('expo/metro-config');
const http = require('node:http');

const config = getDefaultConfig(__dirname);
const enhance = config.server.enhanceMiddleware;
config.server.enhanceMiddleware = (base, server) => {
  const next = enhance ? enhance(base, server) : base;
  return (req, res, done) => {
    if (req.url !== '/codeweft/mobile-link') return next(req, res, done);
    if (req.method !== 'POST' || req.headers.origin || req.headers['content-type'] !== 'application/json') {
      res.writeHead(403).end();
      return;
    }
    const chunks = [];
    let size = 0;
    req.on('data', chunk => {
      size += chunk.length;
      if (size <= 165000) chunks.push(chunk);
      else if (!res.writableEnded) res.writeHead(413).end();
    });
    req.on('end', () => {
      if (res.writableEnded) return;
      // Metro 只把密文转发到固定的工作台入口，两端的请求头和桌面会话 Cookie 均在这里隔离。
      const upstream = http.request({
        hostname: '127.0.0.1', port: process.env.CODEWEFT_WEB_PORT || 8765,
        path: '/mobile-link/exchange', method: 'POST', headers: { 'Content-Type': 'application/json' },
      }, response => {
        res.writeHead(response.statusCode, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
        response.pipe(res);
      });
      upstream.setTimeout(20000, () => upstream.destroy());
      upstream.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end(); });
      upstream.end(Buffer.concat(chunks));
    });
  };
};
module.exports = config;
