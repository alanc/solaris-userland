#!/usr/bin/python3.7
#
# CDDL HEADER START
#
# The contents of this file are subject to the terms of the
# Common Development and Distribution License (the "License").
# You may not use this file except in compliance with the License.
#
# You can obtain a copy of the license at usr/src/OPENSOLARIS.LICENSE
# or http://www.opensolaris.org/os/licensing.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# When distributing Covered Code, include this CDDL HEADER in each
# file and include the License file at usr/src/OPENSOLARIS.LICENSE.
# If applicable, add the following below this CDDL HEADER, with the
# fields enclosed by brackets "[]" replaced with your own identifying
# information: Portions Copyright [yyyy] [name of copyright owner]
#
# CDDL HEADER END
#

#
# Copyright (c) 2010, 2021, Oracle and/or its affiliates.
#

# Some userland consolidation specific lint checks

#
# Few notes for those extending lint check.
#
# All methods that should be called by pkglint must have a pkglint_id argument
# with a distinct default value. Since pkglint calls all methods with pkglint_id,
# you cannot use this argument name in any additional methods (you can use
# e.g. _pkglint_id instead).
#
# There are two different checker base classes provided by pkglint:
# base.ManifestChecker
#   lint methods are called once for the netire manifest
# base.ActionChecker
#   lint methods are called for each action (which is each line of the manifest)
#
# arguments passed into each method are implemented in the following places:
#   action:  /ips-gate/src/modules/actions/*
#   engine:  /ips-gate/src/modules/lint/engine.py
#   manifest:  /ips-gate/src/modules/manifest.py
#

import os.path
import platform
import re
import subprocess
import sys

import pkg.elf as elf
import pkg.fmri
import pkg.lint.base as base

from pkg.lint.engine import lint_fmri_successor


class UserlandActionChecker(base.ActionChecker):
    """An opensolaris.org-specific class to check actions."""

    name = "userland.action"

    def __init__(self, config):
        self.description = "checks Userland packages for common content errors"
        path = os.getenv("PROTO_PATH")
        if path:
            self.proto_path = path.split()
        else:
            self.proto_path = None
        solaris_ver = os.getenv("SOLARIS_VERSION", "")
        #
        # These lists are used to check if a 32/64-bit binary
        # is in a proper 32/64-bit directory.
        #
        self.pathlist32 = [
            "i86",
            "sparcv7",
            "32",
            "i86pc-solaris-64int",  # perl path
            "sun4-solaris-64int",  # perl path
            "i386-solaris" + solaris_ver,  # ruby path
            "sparc-solaris" + solaris_ver,  # ruby path
        ]
        self.pathlist64 = [
            "amd64",
            "sparcv9",
            "64",
            "fbconfig",  # x11/app/gfx-utils path
            "i86pc-solaris-64",  # perl path
            "sun4-solaris-64",  # perl path
            "i86pc-solaris-thread-multi-64",  # perl path
            "sun4-solaris-thread-multi-64",  # perl path
            "amd64-solaris" + solaris_ver,  # ruby path
            "sparcv9-solaris" + solaris_ver,  # ruby path
            "sparcv9-sun-solaris" + solaris_ver,  # ruby path
            "amd64-solaris-" + solaris_ver,  # ruby path
            "sparcv9-solaris-" + solaris_ver,  # ruby path
            "x86_64-pc-solaris" + solaris_ver,  # GCC path
        ]
        #
        # This list is used to check if all actions are installed
        # in known locations only (Bug 32663975).
        #
        self.allowed_paths = [
            re.compile(r"^boot/"),
            re.compile(r"^etc/"),
            re.compile(r"^usr/"),
            re.compile(r"^lib/"),
            re.compile(r"^kernel/"),
            re.compile(r"^var/(?!share/)")
        ]
        #
        # These lists are used to check if binary RUNPATH points
        # to a proper 32/64-bit directory.
        #
        self.runpath_re = [
            re.compile(r"^/lib(/.*)?$"),
            re.compile(r"^/usr/"),
            re.compile(r"^\$ORIGIN/"),
        ]
        self.runpath_64_re = [
            re.compile(r"^.*/64(/.*)?$"),
            re.compile(r"^.*/amd64(/.*)?$"),
            re.compile(r"^.*/sparcv9(/.*)?$"),
            re.compile(r"^.*/i86pc-solaris-64(/.*)?$"),  # perl path
            re.compile(r"^.*/sun4-solaris-64(/.*)?$"),  # perl path
            re.compile(r"^.*/i86pc-solaris-thread-multi-64(/.*)?$"),
            # perl path
            re.compile(r"^.*/sun4-solaris-thread-multi-64(/.*)?$"),
            # perl path
            re.compile(r"^.*/amd64-solaris2\.[0-9]+(/.*)?$"),
            # ruby path
            re.compile(r"^.*/sparcv9-solaris2\.[0-9]+(/.*)?$"),
            # ruby path
            re.compile(r"^.*/sparcv9-sun-solaris2\.[0-9]+(/.*)?$"),
            # GCC path
            re.compile(r"^.*/x86_64-sun-solaris2\.[0-9]+(/.*)?$"),
            # GCC path
            re.compile(r"^/usr/lib/fbconfig(/)?$"),
            # x11/app/gfx-utils path
            re.compile(r"^/usr/lib/xorg/modules(/)?$"),
            # Xorg path
            re.compile(r"^/usr/lib/xorg/modules/(drivers|extensions|input)$")
            # Xorg path
        ]
        self.initscript_re = re.compile(r"^etc/(rc.|init)\.d")

        self.lint_paths = {}
        self.ref_paths = {}

        super(UserlandActionChecker, self).__init__(config)

    def startup(self, engine):
        """Initialize the checker with a dictionary of paths, so that we
        can do link resolution.

        This is copied from the core pkglint code, but should eventually
        be made common.
        """

        def seed_dict(mf, attr, dic, atype=None, verbose=False):
            """Updates a dictionary of { attr: [(fmri, action), ..]}
            where attr is the value of that attribute from
            actions of a given type atype, in the given
            manifest."""

            pkg_vars = mf.get_all_variants()

            if atype:
                mfg = (a for a in mf.gen_actions_by_type(atype))
            else:
                mfg = (a for a in mf.gen_actions())

            for action in mfg:
                if atype and action.name != atype:
                    continue
                if attr not in action.attrs:
                    continue

                variants = action.get_variant_template()
                variants.merge_unknown(pkg_vars)
                # Action attributes must be lists or strings.
                for k, v in variants.items():
                    if isinstance(v, set):
                        action.attrs[k] = list(v)
                    else:
                        action.attrs[k] = v

                p = action.attrs[attr]
                dic.setdefault(p, []).append((mf.fmri, action))

        # construct a set of FMRIs being presented for linting, and
        # avoid seeding the reference dictionary with any for which
        # we're delivering new packages.
        lint_fmris = {}
        for m in engine.gen_manifests(
                engine.lint_api_inst, release=engine.release, pattern=engine.pattern):
            lint_fmris.setdefault(m.fmri.get_name(), []).append(m.fmri)
        for m in engine.lint_manifests:
            lint_fmris.setdefault(m.fmri.get_name(), []).append(m.fmri)

        engine.logger.debug(_("Seeding reference action path dictionaries."))

        for manifest in engine.gen_manifests(
                engine.ref_api_inst, release=engine.release):
            # Only put this manifest into the reference dictionary
            # if it's not an older version of the same package.
            if not any(
                    lint_fmri_successor(fmri, manifest.fmri)
                    for fmri in lint_fmris.get(manifest.fmri.get_name(), [])):
                seed_dict(manifest, "path", self.ref_paths)

        engine.logger.debug(_("Seeding lint action path dictionaries."))

        # we provide a search pattern, to allow users to lint a
        # subset of the packages in the lint_repository
        for manifest in engine.gen_manifests(
                engine.lint_api_inst, release=engine.release, pattern=engine.pattern):
            seed_dict(manifest, "path", self.lint_paths)

        engine.logger.debug(_("Seeding local action path dictionaries."))

        for manifest in engine.lint_manifests:
            seed_dict(manifest, "path", self.lint_paths)

        self.__merge_dict(
            self.lint_paths, self.ref_paths, ignore_pubs=engine.ignore_pubs)

    def __merge_dict(self, src, target, ignore_pubs=True):
        """Merges the given src dictionary into the target
        dictionary, giving us the target content as it would appear,
        were the packages in src to get published to the
        repositories that made up target.

        We need to only merge packages at the same or successive
        version from the src dictionary into the target dictionary.
        If the src dictionary contains a package with no version
        information, it is assumed to be more recent than the same
        package with no version in the target."""

        for p in src:
            if p not in target:
                target[p] = src[p]
                continue

            def build_dic(arr):
                """Builds a dictionary of fmri:action entries"""
                dic = {}
                for pfmri, action in arr:
                    if pfmri in dic:
                        dic[pfmri].append(action)
                    else:
                        dic[pfmri] = [action]
                return dic

            src_dic = build_dic(src[p])
            targ_dic = build_dic(target[p])

            for src_pfmri in src_dic:
                # we want to remove entries deemed older than
                # src_pfmri from targ_dic.
                for targ_pfmri in targ_dic.copy():
                    sname = src_pfmri.get_name()
                    tname = targ_pfmri.get_name()
                    if lint_fmri_successor(
                            src_pfmri, targ_pfmri, ignore_pubs=ignore_pubs):
                        targ_dic.pop(targ_pfmri)
            targ_dic.update(src_dic)
            l = []
            for pfmri in targ_dic:
                for action in targ_dic[pfmri]:
                    l.append((pfmri, action))
            target[p] = l

    def __realpath(self, path, target):
        """Combine path and target to get the real path."""

        result = os.path.dirname(path)

        for frag in target.split(os.sep):
            if frag == "..":
                result = os.path.dirname(result)
            elif frag in ["", "."]:
                pass
            else:
                result = os.path.join(result, frag)

        return result

    def __elf_aslr_check(self, path, engine, _pkglint_id):
        """Verify that given executable binary is ASLR tagged and enabled."""
        elfinfo = elf.get_info(path)
        if elfinfo["type"] != "exe":
            return

        # get the ASLR tag string for this binary
        res = subprocess.run(
            ["/usr/bin/elfedit", "-r", "-e", "dyn:sunw_aslr", path],
            capture_output=True)

        # No ASLR tag was found; everything must be tagged
        if res.returncode != 0:
            engine.error(f"'{path}' is not tagged for aslr",
                         msgid=f"{self.name}{_pkglint_id}.5")

        # look for "ENABLE" anywhere in the string;
        # warn about binaries which are not ASLR enabled
        elif b"ENABLE" not in res.stdout:
            engine.warning(f"'{path}' does not have aslr enabled",
                           msgid=f"{self.name}{_pkglint_id}.6")

    def __elf_runpath_check(self, path, engine, _pkglint_id):
        """Verify that RUNPATH of given binary is correct."""
        runpath_list = []

        dyninfo = elf.get_dynamic(path)
        elfinfo = elf.get_info(path)
        for runpath in dyninfo.get("runpath", "").split(":"):
            if not runpath:
                continue

            match = False
            for expr in self.runpath_re:
                if expr.match(runpath):
                    match = True
                    break

            if not match:
                runpath_list.append(runpath)

            # Make sure RUNPATH matches against a packaged path.
            # Don't check runpaths starting with $ORIGIN, which
            # is specially handled by the linker.
            elif not runpath.startswith("$ORIGIN/"):

                # Strip out leading and trailing '/' in the
                # runpath, since the reference paths don't start
                # with '/' and trailing '/' could cause mismatches.
                # Check first if there is an exact match, then check
                # if any reference path starts with this runpath
                # plus a trailing slash, since it may still be a link
                # to a directory that has no action because it uses
                # the default attributes.
                relative_dir = runpath.strip("/")
                if relative_dir not in self.ref_paths and not any(
                        key.startswith(relative_dir + "/") for key in self.ref_paths):

                    # If still no match, if the runpath contains
                    # an embedded symlink, emit a warning; it may or may
                    # not resolve to a legitimate path.
                    # E.g., for usr/openwin/lib, usr/openwin->X11 and
                    # usr/X11/lib are packaged, but usr/openwin/lib is not.
                    # Otherwise, runpath is bad; add it to list.
                    pdir = os.path.dirname(relative_dir)
                    while pdir != "":
                        if pdir in self.ref_paths and self.ref_paths[pdir][0][1].name == "link":
                            engine.warning(
                                f"runpath '{runpath}' in '{path}' not found in reference "
                                f"paths but contains symlink at '{pdir}'",
                                msgid=f"{self.name}{_pkglint_id}.3")
                            break
                        pdir = os.path.dirname(pdir)
                    else:
                        runpath_list.append(runpath)

            if elfinfo["bits"] == 32:
                for expr in self.runpath_64_re:
                    if expr.search(runpath):
                        engine.warning(
                            f"64-bit runpath in 32-bit binary, '{path}' includes '{runpath}'",
                            msgid=f"{self.name}{_pkglint_id}.3")
            else:
                for expr in self.runpath_64_re:
                    if expr.search(runpath):
                        break
                else:
                    engine.warning(
                        f"32-bit runpath in 64-bit binary, '{path}' includes '{runpath}'",
                        msgid=f"{self.name}{_pkglint_id}.3")

        # handle all incorrect RUNPATHs in a single error
        if runpath_list:
            engine.error(
                f"bad RUNPATH, '{path}' includes '{':'.join(runpath_list)}'",
                msgid=f"{self.name}{_pkglint_id}.3")

    def __elf_location_check(self, path, inspath, engine, _pkglint_id):
        """Make sure that file is placed within correct 32/64 directory."""
        elfinfo = elf.get_info(path)
        bits = elfinfo["bits"]
        elftype = elfinfo["type"]
        elems = os.path.dirname(inspath).split("/")

        path32 = False
        path64 = False

        # Walk through the path elements backward and at the first
        # 32/64 bit specific element, flag it and break.
        for part in elems[::-1]:
            if part in self.pathlist32:
                path32 = True
                break
            if part in self.pathlist64:
                path64 = True
                break

        # The Xorg module directory is a hybrid case - everything
        # but the dri subdirectory is 64-bit
        dirname = os.path.dirname(inspath)
        if dirname.startswith("usr/lib/xorg/modules") and dirname != "usr/lib/xorg/modules/dri":
            path64 = True

        # ignore 64-bit executables in normal (non-32-bit-specific)
        # locations, that's ok now.
        if elftype == "exe" and bits == 64 and not path32 and not path64:
            return

        if bits == 32 and path64:
            engine.error(f"32-bit object '{inspath}' in 64-bit path({elems})",
                         msgid=f"{self.name}{_pkglint_id}.2")
        elif bits == 64 and not path64:
            engine.error(f"64-bit object '{inspath}' in 32-bit path",
                         msgid=f"{self.name}{_pkglint_id}.2")

    def file_action(self, action, manifest, engine, pkglint_id="001"):
        """Various file checks."""

        if action.name not in ["file"]:
            return

        # path to the delivered file
        inspath = action.attrs["path"]

        # path to the file within the prototype area
        path = action.hash
        if path is None or path == "NOHASH":
            path = inspath

        # verify that preserve attribute is correctly used when
        # file is writable and not in other cases
        if "mode" in action.attrs:
            mode = action.attrs["mode"]
            writable = bool(int(mode, 8) & 0o222)

            if writable and "preserve" not in action.attrs:
                engine.error(
                    f"{path} is writable ({mode}), but missing a preserve attribute",
                    msgid=f"{self.name}{pkglint_id}.0")
            elif not writable and "preserve" in action.attrs:
                engine.error(
                    f"{path} has a preserve action, but is not writable ({mode})",
                    msgid=f"{self.name}{pkglint_id}.4")

        elif "preserve" in action.attrs:
            engine.error(f"{path} has a preserve action, but no mode",
                         msgid=f"{self.name}{pkglint_id}.3")

        # checks that require a physical file to look at
        if self.proto_path is not None:
            for directory in self.proto_path:
                fullpath = directory + "/" + path

                if os.path.exists(fullpath):
                    break

            if not os.path.exists(fullpath):
                engine.info(
                    f"{path} missing from proto area, skipping content checks",
                    msgid=f"{self.name}{pkglint_id}.1")
            elif elf.is_elf_object(fullpath):
                # 32/64 bit in wrong place
                self.__elf_location_check(fullpath, inspath, engine, pkglint_id)
                # verify that correct RUNPATH is present
                self.__elf_runpath_check(fullpath, engine, pkglint_id)
                # verify that ASLR is enabled when appropriate
                self.__elf_aslr_check(fullpath, engine, pkglint_id)

    file_action.pkglint_desc = "Paths should exist in the proto area."

    def link_resolves(self, action, manifest, engine, pkglint_id="002"):
        """Checks for link resolution."""

        if action.name not in ["link", "hardlink"]:
            return

        path = action.attrs["path"]
        target = action.attrs["target"]
        realtarget = self.__realpath(path, target)

        # Check against the target image (ref_paths), since links might
        # resolve outside the packages delivering a particular
        # component.

        # links to files should directly match a patch in the reference
        # repo.
        if self.ref_paths.get(realtarget, None):
            return

        # If it didn't match a path in the reference repo, it may still
        # be a link to a directory that has no action because it uses
        # the default attributes.  Look for a path that starts with
        # this value plus a trailing slash to be sure this it will be
        # resolvable on a fully installed system.
        realtarget += "/"
        for key in self.ref_paths:
            if key.startswith(realtarget):
                return

        engine.error(
            f"{action.name} {path} has unresolvable target '{target}'",
            msgid=f"{self.name}{pkglint_id}.0")

    link_resolves.pkglint_desc = "links should resolve."

    def init_script(self, action, manifest, engine, pkglint_id="003"):
        """Checks for SVR4 startup scripts."""

        if action.name not in ["file", "dir", "link", "hardlink"]:
            return

        path = action.attrs["path"]
        if self.initscript_re.match(path):
            engine.warning(
                f"SVR4 startup '{path}', deliver SMF" " service instead",
                msgid=f"{self.name}{pkglint_id}.0")

    init_script.pkglint_desc = "SVR4 startup scripts should not be delivered."

    def delivery_location(self, action, manifest, engine, pkglint_id="004"):
        """Checks if all actions are installed in known locations only."""

        if action.name not in ["file", "dir", "link", "hardlink"]:
            return

        # path to the delivered file
        inspath = action.attrs["path"]

        for expr in self.allowed_paths:
            if expr.match(inspath):
                return
        else:
            # we didn't match anything
            engine.error(f"object delivered into non-standard location: {inspath}",
                         msgid=f"{self.name}{pkglint_id}.0")

    def legacy_action(self, action, manifest, engine, pkglint_id="005"):
        """Checks for deprecated legacy actions."""

        if action.name not in ["legacy"]:
            return

        engine.error("legacy actions are deprecated",
                     msgid=f"{self.name}{pkglint_id}.0")

    legacy_action.pkglint_desc = "Legacy actions are deprecated."


class UserlandManifestChecker(base.ManifestChecker):
    """An opensolaris.org-specific class to check manifests."""

    name = "userland.manifest"

    def component_check(self, manifest, engine, pkglint_id="001"):
        """Make sure that manifests contain license action."""
        if not next(manifest.gen_actions_by_type("file"), False):
            # skip manifests without any files
            return

        # verify that manifest contains at least one license action
        if not next(manifest.gen_actions_by_type("license"), False):
            engine.error("missing license action",
                         msgid=f"{self.name}{pkglint_id}.0")

        # verify that ARC data are present
        if "org.opensolaris.arc-caseid" not in manifest:
            engine.error("missing ARC data (org.opensolaris.arc-caseid)",
                         msgid=f"{self.name}{pkglint_id}.0")

    component_check.pkglint_desc = (
        "License actions and ARC information are required if you deliver files.")

    def publisher_in_fmri(self, manifest, engine, pkglint_id="002"):
        """Verify that manifest publisher is in allowed set."""
        lint_id = f"{self.name}{pkglint_id}"

        # get allowed publishers defined in pkglintrc
        allowed_pubs = engine.get_param(f"{lint_id}.allowed_pubs").split(" ")

        fmri = manifest.fmri
        if fmri.publisher and fmri.publisher not in allowed_pubs:
            engine.error(f"package {manifest.fmri} has a publisher set!",
                         msgid=f"{self.name}{pkglint_id}.2")

    publisher_in_fmri.pkglint_desc = (
        "Publishers mentioned in package FMRIs must be in fixed set.")

    def uses_cffi(self, manifest, engine, pkglint_id="003"):
        """CFFI names the modules it creates with a hash that includes the
        version of CFFI (since the schema may change from one version to
        another).  This means that if a package depends on CFFI, then it must
        also incorporate the version it builds with, which should be the
        version in the gate."""

        cffi_require = None
        cffi_incorp = None
        for action in manifest.gen_actions_by_type("depend"):
            if not any(
                    f
                    for f in action.attrlist("fmri")
                    if "/cffi-" in pkg.fmri.PkgFmri(f, "5.12").pkg_name):
                continue
            if action.attrs["type"] in ("require", "require-any"):
                cffi_require = action
            elif action.attrs["type"] == "incorporate":
                cffi_incorp = action

        if not cffi_require:
            return

        try:
            # Get version of cffi from the proto area. This is done
            # by extending PATH with cffi proto area and importing cffi.
            sys.path[0:0] = [
                os.path.join(
                    os.getenv("WS_TOP", ""),
                    f"components/python/cffi/build/prototype/{platform.processor()}"
                    f"/usr/lib/python{sys.version_info[0]}.{sys.version_info[1]}/"
                    "vendor-packages")
            ]
            import cffi

            cffi_version = cffi.__version__
            del sys.path[0]
        except ImportError:
            cffi_version = None

        if not cffi_version:
            engine.warning(
                f"package {manifest.fmri} depends on CFFI, but we "
                "cannot determine the version of CFFI needed",
                msgid=f"{self.name}{pkglint_id}.1")

        if not cffi_incorp:
            engine.error(
                f"package {manifest.fmri} depends on CFFI, but "
                f"does not incorporate it (should be at {cffi_version})",
                msgid=f"{self.name}{pkglint_id}.2")

        # The final check can only be done if neither of the previous
        # checks have fired.
        if not cffi_version or not cffi_incorp:
            return

        cffi_incorp_ver = str(
            pkg.fmri.PkgFmri(cffi_incorp.attrs["fmri"], "5.12").version.release)
        if cffi_incorp_ver != cffi_version:
            engine.error(
                f"package {manifest.fmri} depends on CFFI, but incorporates it at "
                f"the wrong version ({cffi_incorp_ver} instead of {cffi_version})",
                msgid=f"{self.name}{pkglint_id}.3")

    uses_cffi.pkglint_desc = (
        "Packages using CFFI incorporate CFFI at the correct version.")

    def makefile_var_check(self, manifest, engine, pkglint_id="004"):
        """Make sure that all manifest variables are expanded."""
        for line in manifest.as_lines():
            if "$(" in line:
                engine.error(
                    f"Unexpanded make variable in {manifest.fmri}:\n{line}",
                    msgid=f"{self.name}{pkglint_id}.0")

    makefile_var_check.pkglint_desc = "Unexpanded makefile variable."

    def check_package_arch(self, manifest, engine, pkglint_id="005"):
        """Make sure that the manifests deliver only variant.arch equal to
        architecture on current machine. Otherwise we may have problems later
        when merging i386 and sparc repos together.
        """
        arch = platform.processor()

        # First make sure that whole manifest does not have
        # wrong variant.arch set
        for variant, values in manifest.gen_variants():
            if variant != "variant.arch":
                continue

            if set(values) == set([arch]):
                continue

            engine.error(
                f"Package {manifest.fmri} is being published for wrong "
                f"architecture {values} instead of {arch}:\n{(variant, values)}\n",
                msgid=f"{self.name}{pkglint_id}.1")

        # Then go through all actions in the manifest
        for action in manifest.gen_actions():
            for variant in action.get_variant_template():
                if variant != "variant.arch":
                    continue

                values = action.attrs[variant]
                if set(values) == set([arch]):
                    continue

                engine.error(
                    f"The manifest {manifest.fmri} contains action with wrong "
                    f"architecture '{values}' (instead of '{arch}'):\n{action}\n",
                    msgid=f"{self.name}{pkglint_id}.2")

    check_package_arch.pkglint_desc = "Wrong architecture package."
