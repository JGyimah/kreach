# A finding nobody asked for

kreach turned this up while running the historical-recall benchmark, at the
parent of `2182f358fb13` — a commit that fixes a *different* dependency in the
same driver. It is not the bug that commit addressed and it is not in the
benchmark, so the benchmark scores that case as a miss, correctly. The finding
is real anyway.

| | |
|---|---|
| caller | `drivers/gpu/drm/xe/*.o` → `CONFIG_DRM_XE` |
| provider | `drivers/gpu/drm/xe/xe_configfs.o`, part of `xe.ko` → `CONFIG_CONFIGFS_FS` |
| case | `y/m` — xe built into vmlinux, configfs a module |
| confirmed | **v7.3, `ARCH=x86_64`**, link fails in 245 s on eleven symbols |
| reproducer | [`drm_xe.config`](drm_xe.config) |
| evidence | [`confirm-v7.3.log`](confirm-v7.3.log) |

## The mechanism

`drivers/gpu/drm/xe/Makefile` pulls the part in on configfs:

```
xe-$(CONFIG_CONFIGFS_FS) += xe_configfs.o
```

With `CONFIG_DRM_XE=y` and `CONFIG_CONFIGFS_FS=m` that line becomes
`xe-m += xe_configfs.o`, which Kbuild ignores for a built-in module — it uses
`xe-y`. The part is dropped while the rest of xe still calls into it:

```
xe_guc.c:(.text+0x217e): undefined reference to `xe_configfs_get_psmi_enabled'
ld: xe_guc.c:(.text+0x21d9): undefined reference to `xe_configfs_get_enable_multi_queue'
xe_guc_ads.c:(.text+0x1a28): undefined reference to `xe_configfs_get_enable_multi_queue'
```

Eleven symbols in all, from `xe_module.o`, `xe_guc.o` and `xe_guc_ads.o`. At
the v6.15-era parent where kreach first found it there were four, from
`xe_module.o` alone — the surface has grown as call sites were added.

## The fix

```
depends on CONFIGFS_FS || !CONFIGFS_FS
```

which reads "if configfs is a module, I must be a module too". The `DRM_XE`
stanza already carries that idiom three times, for `KUNIT`, `INTEL_VSEC` and
`X86_PLATFORM_DEVICES`; `CONFIGFS_FS` is simply missing from the list.
Verified: with that line added, Kconfig can no longer produce the
combination from the reproducer fragment.

Note this is *not* what kreach suggests. Its `depends on CONFIGFS_FS` would
cap `DRM_XE` at configfs's level, forcing xe to be a module whenever configfs
is — the right shape, the wrong strength. The specification's rule against
suggesting `select` gets the form right and leaves the degree to a human.

```
Fixes: 16280ded45fb ("drm/xe: Add configfs to enable survivability mode")
```

verified as the commit that added `xe_configfs.c`.
