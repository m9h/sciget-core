# sciget-core

Start and manage [Sciget](https://github.com/scigetorg/neurodesk-app)/Neurodesk
container sessions from the command line or a local web UI, with no Electron.

sciget-core is the engine half of the Sciget App, split out so it can be used
headless (scripts, HPC, teaching) and packaged natively, for example as a noarch
Fedora RPM. It needs only Python 3.10+, PyYAML and platformdirs, plus one of
Podman, Docker or Apptainer.

```console
$ sciget engines
podman     available 5.8.9 rootless
apptainer  available 1.5.5 rootless
$ sciget start ~/myproject --open          # mounts ~/myproject at /data
Session 9e3e5c0b is ready:
  http://127.0.0.1:35827/lab?token=...
$ sciget list
$ sciget stop 9e3e5c0b
$ sciget start --dry-run                   # print the engine command only
$ sciget serve --open                      # web UI and local API (docs/api.md)
```

## How it differs from Sciget App 2.2.0

| Sciget App 2.2.0 | sciget-core |
|---|---|
| Every container runs `--privileged --user=root` | Not privileged. CVMFS comes from the host (`--cvmfs host`), or the container gets `/dev/fuse` and `CAP_SYS_ADMIN` only. `--privileged` is an explicit option. |
| Commands assembled as shell strings | Argument vectors; nothing goes through a shell |
| Jupyter token on the command line | Token passed in the environment (not visible in `ps`) |
| Fixed IP 10.88.0.10 and MAC 88:75:56:ef:3e:d6 | No fixed address; `--mac-address` per session for licensed software |
| `killall qemu-system-*` on shutdown | Stops only its own container or process group |
| Podman `--cgroups=disabled` | cgroups left on; rootless Podman uses `--userns keep-id` |
| One session per name and user | Each session has its own name, port and token |
| GUI only | CLI, Python API and a loopback HTTP API with token, Host and Origin checks |
| No HPC engine | Apptainer engine (no root, shares the host network on 127.0.0.1) |

Under SELinux, containers get `--security-opt label=disable` because the
user's folders and `/cvmfs` cannot be relabelled. Namespaces, capabilities and
seccomp still apply.

## CVMFS modes

- `auto` (the default): `host` if `/cvmfs/<repository>` is mounted on this
  computer, otherwise `container` (`off` under Apptainer).
- `host`: bind-mounts the host's `/cvmfs` read-only. Needs the cvmfs client
  and the Neurodesk repository configuration installed on the host.
- `container`: the image mounts CVMFS itself with FUSE.
- `off`: tools are downloaded on first use instead of streamed.

## Domains

Domain files use the same keys as upstream's `container_installer/*.yml`.
Built-in domains are in `src/sciget_core/domains/`. Files in
`~/.config/sciget/domains/` add new domains or override built-in ones.

## Status

Version 0.1.0, not yet tested against the real Neurodesk image. In particular,
the unprivileged `container` CVMFS mode, rootless `keep-id` file ownership and
the Apptainer engine still need testing end to end.

## Licence

BSD-3-Clause.
