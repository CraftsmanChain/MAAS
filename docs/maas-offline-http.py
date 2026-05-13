#!/usr/bin/env python3

import argparse
import http.server
import os
import posixpath
import socketserver
import sys
import urllib.parse


def parse_maps(values):
    out = []
    for v in values:
        if "=" not in v:
            raise ValueError(v)
        prefix, directory = v.split("=", 1)
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        if not prefix.endswith("/"):
            prefix = prefix + "/"
        directory = os.path.abspath(directory)
        out.append((prefix, directory))
    out.sort(key=lambda x: len(x[0]), reverse=True)
    return out


class MappedHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        return super().do_GET()

    def do_HEAD(self):
        return super().do_HEAD()

    def translate_path(self, path):
        raw = urllib.parse.urlparse(path).path
        raw = posixpath.normpath(raw)
        if not raw.startswith("/"):
            raw = "/" + raw
        if raw != "/" and path.endswith("/"):
            raw += "/"

        chosen = None
        rel = None
        for prefix, directory in self.server.path_maps:
            if raw == prefix[:-1] or raw.startswith(prefix):
                chosen = directory
                rel = raw[len(prefix) - 1 :]
                break
        if chosen is None:
            return "/__maas_offline_http_404__"

        rel = posixpath.normpath(rel)
        parts = [p for p in rel.split("/") if p and p not in (".", "..")]
        full = chosen
        for p in parts:
            full = os.path.join(full, p)
        return full

    def send_head(self):
        path = self.translate_path(self.path)
        if path.endswith("/__maas_offline_http_404__"):
            self.send_error(404, "Not Found")
            return None
        return super().send_head()


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8083)
    ap.add_argument("--map", action="append", default=[])
    args = ap.parse_args()

    if not args.map:
        print("missing --map", file=sys.stderr)
        return 2

    maps = parse_maps(args.map)
    for _, d in maps:
        if not os.path.isdir(d):
            print(f"directory not found: {d}", file=sys.stderr)
            return 2

    httpd = ThreadingHTTPServer((args.bind, args.port), MappedHandler)
    httpd.path_maps = maps

    print(f"bind={args.bind} port={args.port}")
    for p, d in maps:
        print(f"{p} -> {d}")

    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

