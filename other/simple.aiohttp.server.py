#!/usr/bin/env python3

# Use 'pdb' to explore the inner workings of 'aiohttp'.

import pdb
from aiohttp import web


async def handle_get(request):
    #pdb.set_trace()
    name = request.match_info.get('name', "Anonymous")
    text = "Hello, " + name
    return web.Response(text=text)

async def handle_post(request):
    text = await request.text()
    data = await request.post()
    #pdb.set_trace()
    text = "Hello, " + data.get("name", "Anonymous")
    return web.Response(text=text)

@web.middleware
async def test_mw(request, handler):
    print("url: ", request.url)
    print("forwarded: ", request.forwarded)
    print("headers: ", request.headers)
    body = await request.read()
    post = await request.post()
    print("body: ", body)
    print("post: ", post)
    #pdb.set_trace()
    request = await handler(request)
    request.text += " (validated)"
    return request

app = web.Application(middlewares=[test_mw])

app.add_routes(
    [ web.get("/", handle_get)
    , web.get("/{name}", handle_get)
    , web.post("/post", handle_post)
    ])

if __name__ == "__main__":
    web.run_app(app)
