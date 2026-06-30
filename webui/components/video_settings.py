import streamlit as st
from app.config import config
from app.models.schema import VideoClipParams, VideoAspect, AudioVolumeDefaults


def render_video_panel(tr):
    """渲染视频配置面板"""
    with st.container(border=True):
        st.write(tr("Video Settings"))
        params = VideoClipParams()
        render_video_config(tr, params)


def render_video_config(tr, params):
    """渲染视频配置"""
    # 视频比例
    video_aspect_ratios = [
        (tr("Portrait"), VideoAspect.portrait.value),
        (tr("Landscape"), VideoAspect.landscape.value),
    ]
    default_aspect = getattr(params, "video_aspect", VideoAspect.landscape.value)
    default_aspect = getattr(default_aspect, "value", default_aspect)
    default_index = next(
        (
            index
            for index, (_, aspect) in enumerate(video_aspect_ratios)
            if aspect == default_aspect
        ),
        1,
    )
    selected_index = st.selectbox(
        tr("Video Ratio"),
        options=range(len(video_aspect_ratios)),
        format_func=lambda x: video_aspect_ratios[x][0],
        index=default_index,
    )
    params.video_aspect = VideoAspect(video_aspect_ratios[selected_index][1])
    st.session_state['video_aspect'] = params.video_aspect.value

    # 视频画质
    video_qualities = [
        ("4K (2160p)", "2160p"),
        ("2K (1440p)", "1440p"),
        ("Full HD (1080p)", "1080p"),
        ("HD (720p)", "720p"),
        ("SD (480p)", "480p"),
    ]
    quality_index = st.selectbox(
        tr("Video Quality"),
        options=range(len(video_qualities)),
        format_func=lambda x: video_qualities[x][0],
        index=2  # 默认选择 1080p
    )
    st.session_state['video_quality'] = video_qualities[quality_index][1]

    # 原声音量 - 使用统一的默认值
    params.original_volume = st.slider(
        tr("Original Volume"),
        min_value=AudioVolumeDefaults.MIN_VOLUME,
        max_value=AudioVolumeDefaults.MAX_VOLUME,
        value=AudioVolumeDefaults.ORIGINAL_VOLUME,
        step=0.01,
        help=tr("Adjust the volume of the original audio")
    )
    st.session_state['original_volume'] = params.original_volume


def get_video_params():
    """获取视频参数"""
    return {
        'video_aspect': st.session_state.get('video_aspect', VideoAspect.landscape.value),
        'video_quality': st.session_state.get('video_quality', '1080p'),
        'original_volume': st.session_state.get('original_volume', AudioVolumeDefaults.ORIGINAL_VOLUME),
        'cover_enabled': config.ui.get('cover_enabled', False),
        'cover_api_url': config.ui.get('cover_api_url', 'http://127.0.0.1:8080'),
        'cover_name': (
            st.session_state['cover_name']
            if 'cover_name' in st.session_state
            else config.ui.get('cover_name', '')
        ),
        'cover_platforms': config.ui.get('cover_platforms', []),
        'cover_style_hint': config.ui.get('cover_style_hint', ''),
        'cover_use_llm': config.ui.get('cover_use_llm', True),
    }
