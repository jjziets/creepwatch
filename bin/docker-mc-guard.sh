#!/bin/sh
# Restricted docker(1) for mc-guard: only the minecraft container / compose service.
# Real CLI from the host is at DOCKER_REAL (default /usr/bin/docker.real).
# Invoked as "docker" with normal argv: docker <subcommand> ...

set -eu

REAL="${DOCKER_REAL:-/usr/bin/docker.real}"

if [ ! -x "$REAL" ]; then
  echo "docker-mc-guard: real docker not executable: $REAL" >&2
  exit 14
fi

deny() {
  echo "docker-mc-guard: denied (mc-guard may only manage container/service 'minecraft')" >&2
  exit 13
}

# shellcheck disable=SC2317
validate_compose() {
  [ "$1" = compose ] || return 1
  shift
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --project-directory|-f)
        [ "$#" -ge 2 ] || return 1
        shift 2
        ;;
      pull)
        shift
        [ "$#" -eq 1 ] || return 1
        [ "$1" = minecraft ] || return 1
        return 0
        ;;
      up)
        shift
        d=0
        nd=0
        mc=0
        for a do
          case "$a" in
            -d) d=1 ;;
            --no-deps) nd=1 ;;
            minecraft) mc=1 ;;
            *) return 1 ;;
          esac
        done
        [ "$d" = 1 ] && [ "$nd" = 1 ] && [ "$mc" = 1 ] || return 1
        return 0
        ;;
      *) return 1 ;;
    esac
  done
  return 1
}

[ "$#" -ge 1 ] || deny

case "$1" in
  exec)
    # docker exec minecraft rcon-cli …
    [ "$#" -ge 4 ] || deny
    [ "$2" = minecraft ] || deny
    [ "$3" = rcon-cli ] || deny
    shift 3
    exec "$REAL" exec minecraft rcon-cli "$@"
    ;;
  logs)
    # docker logs [OPTIONS] … minecraft — container name must be last argument
    shift
    [ "$#" -ge 1 ] || deny
    last=""
    for a do last=$a; done
    [ "$last" = minecraft ] || deny
    exec "$REAL" logs "$@"
    ;;
  compose)
    validate_compose "$@" || deny
    exec "$REAL" "$@"
    ;;
  *)
    deny
    ;;
esac
