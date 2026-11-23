#!/bin/sh
set -eu
mkdir -p /tmp/home /run/display /var/cache/hmi /var/log /var/lock
export HOME=/tmp/home FONTCONFIG_PATH=/tsd/hmi LD_LIBRARY_PATH=/tsd/lib
export XDG_RUNTIME_DIR=/run/display TSD_LOGCHANNEL=hmi TSD_COMMON_CONFIG=/etc/nice/hmi.conf
export MALLOC_ARENA_MAX=2 MALLOC_MMAP_THRESHOLD_=131072
if [ "$2" = lab ]; then
    export TSD_COMMON_CONFIG=/lab/nice.conf
    export JVM_JIT_CPU_AFFINITY_MASK=7 JVM_GC_CPU_AFFINITY_MASK=7 JVM_GC_THREAD_COUNT=3
    # Factory CDS rejected this classpath; load the original JARs directly.
    export _JAVA_OPTIONS='-Xpreload:off -XX:-UseSharedSpaces -XX:-CompressedSharedSpaces -XX:-EnableSharedLookupCache -XX:-PrecompilePreloadedClasses'
    # Original ContextInformationWorkUnit exposes these waits for absent lab services.
    export _JAVA_OPTIONS="$_JAVA_OPTIONS -Dde.vw.mib.asl.startup.timeout.dsi=2000 -Dde.vw.mib.asl.startup.timeout.rsi=2000"
    # The viewer sends full-resolution pixels; the factory ABT uses half-resolution coordinates.
    export _JAVA_OPTIONS="$_JAVA_OPTIONS -Dde.vw.mib.asl.keypanel.touch.scaleX=1 -Dde.vw.mib.asl.keypanel.touch.scaleY=1"
    for priority in 1 2 3 4 5 6 7 8 9 10 COMP; do
        export "JVM_THREAD_NATIVE_PRIO_$priority=0"
    done
    export WAYLAND_DEBUG=1
    if [ "${JOYNEXT_GL_FORWARD:-0}" = 1 ]; then
        export LD_LIBRARY_PATH=/bench/glfwd:/tsd/lib
        export MOI3_GL_SOCKET=/run/display/moi3-gl.sock
    fi
    /bench/mock_commgr "$3" > /out/commgr.jsonl 2>&1 &
    for tick in $(seq 1 50); do
        grep -q '"action":"ready"' /out/commgr.jsonl && break
        sleep 0.1
    done
    grep -q '"action":"ready"' /out/commgr.jsonl
    if [ "${JOYNEXT_RSI:-0}" = 1 ]; then
        for tick in $(seq 1 100); do
            [ -f /run/display/rsi-ready ] && break
            sleep 0.1
        done
        test -f /run/display/rsi-ready
    fi
fi
ulimit -c 0
/java/bin/java -version > /out/java-version.txt 2>&1
/lib/ld-linux-aarch64.so.1 --list /java/bin/java > /out/java-loader.txt 2>&1
env | sort > /out/environment.txt
cat /proc/self/status > /out/supervisor-status.txt
cat /proc/self/mountinfo > /out/mountinfo.txt
cd /tsd/hmi
set +e
/usr/bin/strace -ff -tt -s 160 -e trace=process,network,ioctl,openat -o /out/strace /bin/sh /tsd/hmi/runHMI.sh > /out/hmi.log 2>&1 &
trace_pid=$!
for tick in $(seq 1 "$1"); do
    [ ! -f /run/display/stop ] || break
    for exe in /proc/[0-9]*/exe; do
        if [ "$(readlink "$exe" 2>/dev/null)" = /java/bin/javalauncher ]; then
            proc=${exe%/exe}
            echo "${proc##*/}" > /out/java-pid.txt
            cat "$proc/maps" > /out/java-maps.txt
            cat "$proc/status" > /out/java-status.txt
        fi
    done
    kill -0 "$trace_pid" 2>/dev/null || break
    sleep 1
done
echo 0 > /out/java-alive-at-stop.txt
if [ -f /out/java-pid.txt ]; then
    proc=/proc/$(cat /out/java-pid.txt)
    if [ -r "$proc/maps" ]; then
        echo 1 > /out/java-alive-at-stop.txt
        cat "$proc/maps" > /out/java-maps.txt
        cat "$proc/status" > /out/java-status.txt
        awk '$2 ~ /x/ && $6 ~ /^\// {print $6}' /out/java-maps.txt | sort -u | while read path; do
            sha256sum "$path"
        done > /out/java-mapped-sha256.txt
        sha256sum /tsd/hmi/runHMI.sh /tsd/hmi/jar/MIBHMI-STD-H40.272.318-bin.jar > /out/hmi-files-sha256.txt
    fi
fi
# Container teardown terminates the watchdog and any remaining HMI threads.
if kill -0 "$trace_pid" 2>/dev/null; then
    if [ -f /run/display/stop ]; then
        echo requested-stop > /out/outcome.txt
    else
        echo bounded-stop > /out/outcome.txt
    fi
else
    wait "$trace_pid"
    echo "hmi-exit:$?" > /out/outcome.txt
fi
exit 0
