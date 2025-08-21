import os
import sys
import subprocess
import threading
import mutagen
from mutagen.mp4 import MP4, MP4Cover
from mutagen.id3 import APIC
from tqdm import tqdm


def copy_metadata(source_path, dest_path):
    """
    다양한 오디오 파일(FLAC, MP3, WAV)의 메타데이터와 앨범 아트를 M4A로 복사합니다.
    """
    try:
        source_audio = mutagen.File(source_path)
        if source_audio is None:
            # 파일을 읽을 수 없는 경우
            tqdm.write(f"  !! 메타데이터 읽기 실패 (파일 지원 안됨): {os.path.basename(source_path)}")
            return

        # 1. 태그 정보 추출 (표준화된 딕셔너리로)
        tags = {}
        # 'easy=True'는 MP3, FLAC, M4A, OGG 등에서 간단한 태그 접근을 제공
        easy_source = mutagen.File(source_path, easy=True)
        if easy_source is not None:
            for key in ['title', 'artist', 'album', 'albumartist', 'genre', 'date', 'tracknumber', 'discnumber',
                        'comment']:
                if key in easy_source:
                    tags[key] = easy_source[key][0]

        # 2. 앨범 아트 추출
        picture = None
        if isinstance(source_audio, mutagen.mp3.MP3):
            # MP3 (ID3) 앨범 아트
            if 'APIC:' in source_audio:
                picture = source_audio.tags['APIC:']
        elif isinstance(source_audio, mutagen.flac.FLAC):
            # FLAC 앨범 아트
            if source_audio.pictures:
                picture = source_audio.pictures[0]

        # 3. M4A 파일에 메타데이터 및 앨범 아트 쓰기
        m4a = MP4(dest_path)
        m4a.clear()

        tag_map = {
            'title': '©nam', 'artist': '©ART', 'album': '©alb',
            'albumartist': 'aART', 'genre': '©gen', 'date': '©day',
            'comment': '©cmt'
        }
        for key, m4a_key in tag_map.items():
            if key in tags:
                m4a[m4a_key] = [tags[key]]

        # 트랙/디스크 번호 처리
        if 'tracknumber' in tags:
            parts = str(tags['tracknumber']).split('/')
            num = int(parts[0]) if parts[0].isdigit() else 0
            total = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            m4a['trkn'] = [(num, total)]
        if 'discnumber' in tags:
            parts = str(tags['discnumber']).split('/')
            num = int(parts[0]) if parts[0].isdigit() else 0
            total = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            m4a['disk'] = [(num, total)]

        # 앨범 아트 쓰기
        if picture:
            cover_format = MP4Cover.FORMAT_JPEG if picture.mime in ['image/jpeg', 'jpeg'] else MP4Cover.FORMAT_PNG
            m4a['covr'] = [MP4Cover(picture.data, imageformat=cover_format)]

        m4a.save()

    except Exception as e:
        tqdm.write(f"  !! 메타데이터 복사 오류 ({os.path.basename(source_path)}): {e}")


def worker(file_chunk, thread_position, input_folder, output_folder):
    """
    파일 목록(청크)을 받아 순차적으로 변환하는 스레드 작업자 함수.
    """
    for source_path in tqdm(file_chunk, desc=f"Thread-{thread_position + 1}", unit="file", position=thread_position):
        try:
            relative_path = os.path.relpath(source_path, input_folder)
            dest_path_m4a = os.path.join(output_folder, os.path.splitext(relative_path)[0] + '.m4a')
            os.makedirs(os.path.dirname(dest_path_m4a), exist_ok=True)

            command = [
                'ffmpeg', '-i', source_path,
                '-vn', '-c:a', 'aac', '-b:a', '350k',
                '-y', dest_path_m4a
            ]
            subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            copy_metadata(source_path, dest_path_m4a)

        except subprocess.CalledProcessError as e:
            tqdm.write(f"  !! FFmpeg 오류 [{os.path.basename(source_path)}]: {e.stderr.decode('utf-8', errors='ignore')}")
        except Exception as e:
            tqdm.write(f"  !! 처리 중 오류 [{os.path.basename(source_path)}]: {e}")


def convert_audio_to_m4a(input_folder):
    """
    다양한 오디오 파일들을 10개의 그룹으로 나누어 멀티스레드로 M4A로 변환합니다.
    """
    if not os.path.isdir(input_folder):
        print(f"오류: '{input_folder}'는 유효한 폴더가 아닙니다.")
        sys.exit(1)

    base_name = os.path.basename(os.path.normpath(input_folder))
    output_folder = f"{base_name}_Convert"
    os.makedirs(output_folder, exist_ok=True)

    # [변경점] 지원 파일 형식 확장
    supported_extensions = ('.flac', '.mp3', '.wav')
    audio_files = [os.path.join(r, f) for r, _, files in os.walk(input_folder) for f in files if
                   f.lower().endswith(supported_extensions)]

    if not audio_files:
        print(f"지정한 폴더에 지원하는 오디오 파일({', '.join(supported_extensions)})이 없습니다.")
        return

    num_threads = min(10, len(audio_files))
    chunks = [[] for _ in range(num_threads)]
    for i, file in enumerate(audio_files):
        chunks[i % num_threads].append(file)

    print(f"총 {len(audio_files)}개의 오디오 파일을 {num_threads}개의 스레드로 나누어 변환합니다.")

    threads = []
    for i in range(num_threads):
        thread = threading.Thread(target=worker, args=(chunks[i], i, input_folder, output_folder))
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()


if __name__ == "__main__":
    input_directory = "C:\\Users\\jun\\PycharmProjects\\PythonProject\\Music"
    convert_audio_to_m4a(input_directory)
    print("\n" * 10)
    print("✅ 모든 작업이 완료되었습니다.")