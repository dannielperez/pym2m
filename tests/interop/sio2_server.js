// Synthetic Socket.IO 2.5.0 server mimicking the documented live-communications contract.
const http = require('http');
const server = http.createServer();
const io = require('socket.io')(server, { transports: ['websocket'], pingInterval: 2000, pingTimeout: 1000 });
const ns = io.of('/socket');
let connections = 0;
ns.on('connection', (socket) => {
  connections += 1;
  const conn = connections;
  socket.on('authenticate', (creds) => {
    if (!creds || creds.auth !== 'good-token' || creds.pegasus !== 'https://tracking.example.test') {
      socket.emit('_error', 'invalid credentials');
      return;
    }
    socket.emit('_authenticated', { vehicles: [1, 2], assets: [] });
  });
  socket.on('listen', (env, cb) => {
    const ids = [].concat(env.objects);
    const full = ids.map((id) => ({ namespace: 'vehicle-events', object: id, payload: {
      primary_id: id, updates: ['device', 'event'],
      event: { id: id * 1000 + conn, event_time: 1760000000 + conn * 100, lat: 18.4, lon: -66.05, mph: 12, io_ign: true, valid_position: true },
      device: { connection: { online: true }, latest: { data: { hdop: { value: 0.7 } } }, groups: [1, 2, 3] } } }));
    if (typeof cb === 'function') cb(full);
    setTimeout(() => {
      ids.forEach((id) => socket.emit('events', { namespace: 'vehicle-events', object: id, payload: {
        primary_id: id, updates: ['event', 'device'],
        event: { id: id * 1000 + conn + 500, event_time: 1760000000 + conn * 100 + 30, lat: 18.41, lon: -66.06 },
        _event_restore: ['io_ign', 'valid_position', 'hdop'],
        device: { groups: [9] } } }));
      if (conn === 1) setTimeout(() => socket.disconnect(true), 200);  // force a reconnect once
    }, 100);
  });
  socket.on('stop', (env) => socket.emit('_update', 'Stopped listening to vehicle-events for vehicle ' + env.objects));
});
server.listen(0, '127.0.0.1', () => { console.log(server.address().port); });
