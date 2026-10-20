package worker;

import redis.clients.jedis.Jedis;
import redis.clients.jedis.exceptions.JedisConnectionException;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.io.IOException;
import java.io.OutputStream;
import java.sql.*;
import org.json.JSONObject;

class Worker {
  static volatile Jedis redisConn;
  static volatile Connection dbConn;

  public static void main(String[] args) {
    try {
      startHealthServer();
    } catch (IOException e) {
      e.printStackTrace();
      System.exit(1);
    }

    try {
      redisConn = connectToRedis(System.getenv("REDIS_HOST"), System.getenv("REDIS_PASSWORD"));
      dbConn = connectToDB();

      System.err.println("Watching vote queue");

      while (true) {
        String voteJSON = redisConn.blpop(0, "votes").get(1);
        JSONObject voteData = new JSONObject(voteJSON);
        String voterID = voteData.getString("voter_id");
        String vote = voteData.getString("vote");

        System.err.printf("Processing vote for '%s' by '%s'\n", vote, voterID);
        try {
          updateVote(dbConn, voterID, vote);
        } catch (SQLException e) {
          try {
            redisConn.lpush("votes", voteJSON);
          } catch (Throwable requeueError) {
            requeueError.printStackTrace();
          }
          throw e;
        }
      }
    } catch (Throwable e) {
      e.printStackTrace();
      System.exit(1);
    }
  }

  static void updateVote(Connection dbConn, String voterID, String vote) throws SQLException {
    try (PreparedStatement upsert = dbConn.prepareStatement(
        "INSERT INTO votes (id, vote) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET vote = EXCLUDED.vote")) {
      upsert.setString(1, voterID);
      upsert.setString(2, vote);
      upsert.executeUpdate();
    }
  }

  static Jedis connectToRedis(String host, String password) {
    Jedis conn = new Jedis(host, 6379);

    while (true) {
      try {
        if (password != null && !password.isEmpty()) {
          conn.auth(password);
        }
        conn.ping();
        break;
      } catch (JedisConnectionException e) {
        System.err.printf("Waiting for redis: %s%n", e.getMessage());
        sleep(1000);
      }
    }

    System.err.println("Connected to redis");
    return conn;
  }

  static Connection connectToDB() throws SQLException {
    Connection conn = null;

    try {

      Class.forName("org.postgresql.Driver");
      String url = "jdbc:postgresql://" + System.getenv("POSTGRES_HOST") + ':' + System.getenv("POSTGRES_PORT")
          + "/" + System.getenv("POSTGRES_DB");

      while (conn == null) {
        try {
          conn = DriverManager.getConnection(url, System.getenv("POSTGRES_USER"),
              System.getenv("POSTGRES_PASSWORD"));
        } catch (SQLException e) {
          System.err.printf("Waiting for db: %s%n",
              e.getMessage());
          sleep(1000);
        }
      }

    } catch (ClassNotFoundException e) {
      e.printStackTrace();
      System.exit(1);
    }

    System.err.println("Connected to db");
    return conn;
  }

  static void sleep(long duration) {
    try {
      Thread.sleep(duration);
    } catch (InterruptedException e) {
      System.exit(1);
    }
  }

  // Liveness/readiness endpoints for the vote worker, served on port 80 in the
  // background thread HttpServer spins up on start().
  static void startHealthServer() throws IOException {
    HttpServer server = HttpServer.create(new InetSocketAddress(80), 0);

    server.createContext("/healthz", exchange -> {
      byte[] body = "ok".getBytes();
      exchange.sendResponseHeaders(200, body.length);
      try (OutputStream os = exchange.getResponseBody()) {
        os.write(body);
      }
    });

    server.createContext("/readyz", exchange -> {
      boolean ready = isReady();
      byte[] body = (ready ? "ok" : "not ready").getBytes();
      exchange.sendResponseHeaders(ready ? 200 : 503, body.length);
      try (OutputStream os = exchange.getResponseBody()) {
        os.write(body);
      }
    });

    server.start();
  }

  static boolean isReady() {
    return isRedisReady() && isDbReady();
  }

  static boolean isRedisReady() {
    try (Jedis probe = new Jedis(System.getenv("REDIS_HOST"), 6379, 1000)) {
      String password = System.getenv("REDIS_PASSWORD");
      if (password != null && !password.isEmpty()) {
        probe.auth(password);
      }
      return "PONG".equals(probe.ping());
    } catch (Exception e) {
      return false;
    }
  }

  static boolean isDbReady() {
    Connection db = dbConn;
    if (db == null) {
      return false;
    }
    try {
      return db.isValid(2);
    } catch (SQLException e) {
      return false;
    }
  }
}
