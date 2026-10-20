from flask import Flask, render_template, request, make_response, g
from redis import Redis
import os
import socket
import uuid
import json

option_a = os.getenv('OPTION_A', "Ansible")
option_b = os.getenv('OPTION_B', "Chef")
option_c = os.getenv('OPTION_C', "Puppet")
option_d = os.getenv('OPTION_D', "SaltStack")
hostname = socket.gethostname()

app = Flask(__name__)


def get_redis():
    if not hasattr(g, 'redis'):
        g.redis = Redis(
            host=os.environ['REDIS_HOST'],
            password=os.environ.get('REDIS_PASSWORD') or None,
            db=0,
            socket_timeout=5,
        )
    return g.redis


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/readyz")
def readyz():
    try:
        if get_redis().ping():
            return "ok", 200
    except Exception:
        pass
    return "not ready", 503


@app.route("/", methods=['POST', 'GET'])
def hello():
    voter_id = request.cookies.get('voter_id')
    if not voter_id:
        voter_id = uuid.uuid4().hex

    vote = None

    if request.method == 'POST':
        vote = request.form.get('vote')
        if vote not in ('a', 'b', 'c', 'd'):
            return "Invalid vote", 400
        redis = get_redis()
        data = json.dumps({'voter_id': voter_id, 'vote': vote})
        redis.rpush('votes', data)

    resp = make_response(render_template(
        'index.html',
        option_a=option_a,
        option_b=option_b,
        option_c=option_c,
        option_d=option_d,
        hostname=hostname,
        vote=vote,
    ))
    resp.set_cookie('voter_id', voter_id)
    return resp


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=80, threaded=True)
