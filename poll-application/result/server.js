var express = require('express'),
    async = require('async'),
    pg = require("pg"),
    path = require("path"),
    app = express(),
    server = require('http').Server(app),
    io = require('socket.io')(server);

var dbClient = null;

var db_config = {
  host: process.env['POSTGRES_HOST'],
  port: process.env['POSTGRES_PORT'],
  database: process.env['POSTGRES_DB'],
  user: process.env['POSTGRES_USER'],
  password: process.env['POSTGRES_PASSWORD'],
};

io.sockets.on('connection', function (socket) {

  socket.emit('message', { text : 'Welcome!' });

    socket.on('subscribe', function (data) {
    socket.join(data.channel);
  });
});

async.retry(
  {times: 1000, interval: 1000},
  function(callback) {
      const client = new pg.Client(db_config);
      client.connect().then(() => {
        callback(undefined, client);
      }).catch((err) => {
        console.error("Waiting for db", err);
        callback(err, undefined);
      });
  },
  function(err, client) {
    if (err) {
      console.error("Giving up");
      return process.exit(1);
    }
    console.log("Connected to db");
    dbClient = client;
    client.on('error', function(err) {
      console.error("Db connection error: " + err);
      process.exit(1);
    });
    getVotes(client);
  }
);

function getVotes(client) {
  client.query('SELECT vote, COUNT(id) AS count FROM votes GROUP BY vote', [])
  .then((result) => {
    var votes = collectVotesFromResult(result);
    io.sockets.emit("scores", JSON.stringify(votes));
    setTimeout(function() {getVotes(client) }, 1000);
  }).catch((err) => {
    console.error("Error performing query: " + err);
    process.exit(1);
  });
}

function collectVotesFromResult(result) {
  var votes = {a: 0, b: 0, c: 0, d: 0};

  result.rows.forEach(function (row) {
    votes[row.vote] = parseInt(row.count);
  });

  return votes;
}

app.use(function(req, res, next) {
  res.header("Access-Control-Allow-Origin", "*");
  res.header("Access-Control-Allow-Headers", "Origin, X-Requested-With, Content-Type, Accept");
  res.header("Access-Control-Allow-Methods", "PUT, GET, POST, DELETE, OPTIONS");
  next();
});

app.use(express.static(__dirname + '/views'));

app.get('/', function (req, res) {
  res.sendFile(path.resolve(__dirname + '/views/index.html'));
});

app.get('/healthz', function (req, res) {
  res.sendStatus(200);
});

app.get('/readyz', function (req, res) {
  if (!dbClient) {
    return res.sendStatus(503);
  }
  dbClient.query('SELECT 1')
    .then(() => res.sendStatus(200))
    .catch(() => res.sendStatus(503));
});

server.listen(80, function () {
  var port = server.address().port;
  console.log('App running on port ' + port);
});
