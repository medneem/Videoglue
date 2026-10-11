#!/usr/bin/env bash
# Minimal static Windows x64 FFmpeg 7.1.5 and FFprobe cross-build.
set -euo pipefail
ROOT="${1:-$HOME/videoglue-ffmpeg-build}"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$ROOT/src" "$ROOT/prefix" "$ROOT/output"
ROOT=$(realpath "$ROOT")
PREFIX="$ROOT/prefix"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig"
export PKG_CONFIG_PATH="$PKG_CONFIG_LIBDIR"
JOBS="${JOBS:-4}"
clone() {
    local name=$1 url=$2 ref=$3
    if [[ ! -d "$ROOT/src/$name/.git" ]]; then
        git clone --depth 1 --branch "$ref" "$url" "$ROOT/src/$name"
    fi
}
clone ffmpeg https://github.com/FFmpeg/FFmpeg.git n7.1.5
clone x264 https://code.videolan.org/videolan/x264.git stable
X264_COMMIT=b35605ace3ddf7c1a5d67a2eb553f034aef41d55
if ! git -C "$ROOT/src/x264" cat-file -e "$X264_COMMIT^{commit}" 2>/dev/null; then
    git -C "$ROOT/src/x264" fetch --depth 1 origin "$X264_COMMIT"
fi
git -C "$ROOT/src/x264" checkout --detach "$X264_COMMIT"
test "$(git -C "$ROOT/src/x264" rev-parse HEAD)" = "$X264_COMMIT"
clone vidstab https://github.com/georgmartius/vid.stab.git v1.1.1
clone nvcodec https://github.com/FFmpeg/nv-codec-headers.git n12.2.72.0
clone opencl-headers https://github.com/KhronosGroup/OpenCL-Headers.git v2024.10.24
clone opencl-loader https://github.com/KhronosGroup/OpenCL-ICD-Loader.git v2024.10.24

apply_patch_once() {
    local source=$1 patch=$2
    test -s "$patch"
    if ! git -C "$source" apply --reverse --check "$patch" 2>/dev/null; then
        git -C "$source" apply --check "$patch"
        git -C "$source" apply "$patch"
    fi
}
apply_patch_once "$ROOT/src/vidstab" "$SCRIPT_DIR/vidstab-stabilization.patch"
apply_patch_once "$ROOT/src/ffmpeg" "$SCRIPT_DIR/ffmpeg-stabilization.patch"

pushd "$ROOT/src/x264"
./configure --host=x86_64-w64-mingw32 --cross-prefix=x86_64-w64-mingw32- \
    --prefix="$PREFIX" --enable-static --disable-cli --disable-opencl
make -j "$JOBS"
make install
popd

cmake -S "$ROOT/src/vidstab" -B "$ROOT/src/vidstab/build" \
    -DCMAKE_SYSTEM_NAME=Windows -DCMAKE_C_COMPILER=x86_64-w64-mingw32-gcc \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" -DBUILD_SHARED_LIBS=OFF -DUSE_OMP=OFF
cmake --build "$ROOT/src/vidstab/build" -j "$JOBS"
cmake --install "$ROOT/src/vidstab/build"
make -C "$ROOT/src/nvcodec" PREFIX="$PREFIX" install
mkdir -p "$PREFIX/include/CL"
cp "$ROOT/src/opencl-headers/CL/"*.h "$PREFIX/include/CL/"

cmake -S "$ROOT/src/opencl-loader" -B "$ROOT/src/opencl-loader/build" \
    -DCMAKE_SYSTEM_NAME=Windows -DCMAKE_C_COMPILER=x86_64-w64-mingw32-gcc \
    -DOPENCL_ICD_LOADER_HEADERS_DIR="$PREFIX/include" \
    -DOPENCL_ICD_LOADER_BUILD_SHARED_LIBS=OFF -DENABLE_OPENCL_LAYERS=OFF \
    -DBUILD_TESTING=OFF
cmake --build "$ROOT/src/opencl-loader/build" -j "$JOBS"
cp "$ROOT/src/opencl-loader/build/OpenCL.a" "$PREFIX/lib/libOpenCL.a"

pushd "$ROOT/src/ffmpeg"
PATCH="$SCRIPT_DIR/ffmpeg-nvenc-rate.patch"
test -s "$PATCH"
if ! git apply --reverse --check "$PATCH" 2>/dev/null; then
    git apply --check "$PATCH"
    git apply "$PATCH"
fi
./configure --prefix="$PREFIX" --target-os=mingw32 --arch=x86_64 \
    --cross-prefix=x86_64-w64-mingw32- --enable-cross-compile \
    --pkg-config=pkg-config --pkg-config-flags=--static \
    --extra-cflags="-I$PREFIX/include" --extra-ldflags="-L$PREFIX/lib -static" \
    --extra-libs="-lstdc++ -lcfgmgr32 -lruntimeobject -lole32" \
    --enable-static --disable-shared --disable-autodetect --disable-debug \
    --disable-doc --disable-ffplay --enable-ffprobe \
    --disable-network --disable-devices --disable-postproc \
    --enable-indev=lavfi --enable-gpl --enable-libx264 --enable-libvidstab \
    --enable-opencl --enable-ffnvcodec --enable-nvenc --enable-cuda-llvm \
    --disable-encoders --enable-encoder=libx264,h264_nvenc,aac,mpeg4,dvvideo,flac,pcm_s16le,wrapped_avframe,rawvideo \
    --disable-muxers --enable-muxer=mp4,mov,matroska,avi,null,framehash,rawvideo \
    --disable-protocols --enable-protocol=file,pipe \
    --disable-filters \
    --enable-filter=bwdif,bwdif_cuda,yadif_cuda,deshake_opencl,vidstabdetect,vidstabtransform,idet,concat,setpts,asetpts,setsar,fps,format,scale,pad,crop,hwupload,hwupload_cuda,hwdownload,null,anull,anullsrc,atrim,apad,aresample,aformat,transpose,hflip,vflip,color,testsrc2,sine,anoisesrc,interlace,setfield,select,showinfo
make -j "$JOBS" ffmpeg.exe ffprobe.exe
cp ffmpeg.exe "$ROOT/output/ffmpeg.exe"
cp ffprobe.exe "$ROOT/output/ffprobe.exe"
{
    for name in ffmpeg x264 vidstab nvcodec opencl-headers opencl-loader; do
        printf '%s ' "$name"
        git -C "$ROOT/src/$name" rev-parse HEAD
    done
    sha256sum "$ROOT/output/ffmpeg.exe" "$ROOT/output/ffprobe.exe" "$PATCH" \
        "$SCRIPT_DIR/ffmpeg-stabilization.patch" "$SCRIPT_DIR/vidstab-stabilization.patch" \
        "$SCRIPT_DIR/build_ffmpeg.sh"
} > "$ROOT/output/sources.txt"
popd
echo "Built FFmpeg and FFprobe 7.1.5 for Windows x64 in $ROOT/output"
