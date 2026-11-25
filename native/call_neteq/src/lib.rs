//! Serialized C ABI around the pinned WebRTC NetEq Rust port.
//! Input is decoded, timestamped PCM. The device callback is the only clock:
//! fill asks NetEq for 10 ms blocks, retaining the remainder across periods.
use neteq::{AudioFrame, Config, NetEq, RtpHeader, RtpPacketInfo, SdpAudioFormat};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::slice;

pub struct Playout {
    neteq: NetEq,
    rate: usize,
    sequence: u16,
    started: bool,
    frame: AudioFrame,
    offset: usize,
    failed: bool,
}

impl Playout {
    fn new(rate: usize) -> Option<Self> {
        if ![8000, 16000, 32000, 48000].contains(&rate) {
            return None;
        }
        let config = Config {
            sample_rate_hz: rate as i32,
            max_packets_in_buffer: 24,
            max_delay_ms: 240,
            enable_muted_state: true,
            ..Config::default()
        };
        let mut neteq = NetEq::builder(config).build();
        if !neteq.register_payload_type(96, &SdpAudioFormat::new("L16", rate as i32, 1)) {
            return None;
        }
        Some(Self {
            neteq,
            rate,
            sequence: 0,
            started: false,
            frame: AudioFrame::new(),
            offset: 0,
            failed: false,
        })
    }

    fn insert(&mut self, timestamp: u32, samples: &[f32]) -> bool {
        if self.failed || samples.is_empty() || samples.len() > self.rate * 120 / 1000 {
            return false;
        }
        let mut payload = Vec::with_capacity(2 * samples.len());
        for &value in samples {
            let sample = if value.is_finite() {
                (value.clamp(-1.0, 1.0) * 32768.0)
                    .round()
                    .clamp(-32768.0, 32767.0) as i16
            } else {
                0
            };
            payload.extend_from_slice(&sample.to_be_bytes());
        }
        let header = RtpHeader {
            payload_type: 96,
            sequence_number: self.sequence,
            timestamp,
            ssrc: 1,
            ..RtpHeader::default()
        };
        self.sequence = self.sequence.wrapping_add(1);
        if self
            .neteq
            .insert_packet(&header, &payload, RtpPacketInfo::new(&header, None))
            .is_err()
        {
            return false;
        }
        self.started = true;
        true
    }

    fn fill(&mut self, output: &mut [f32]) -> bool {
        self.render(output.len(), |i, value| output[i] = value)
    }

    fn render(&mut self, count: usize, mut write: impl FnMut(usize, f32)) -> bool {
        for i in 0..count {
            write(i, 0.0);
        }
        if self.failed {
            return false;
        }
        if !self.started {
            return true;
        }
        let mut written = 0;
        while written < count {
            if self.offset >= self.frame.data().len() {
                if self.neteq.get_audio(&mut self.frame, None).is_err()
                    || self.frame.sample_rate_hz != self.rate as i32
                    || self.frame.samples_per_channel != self.rate / 100
                {
                    self.failed = true;
                    return false;
                }
                self.offset = 0;
            }
            let frame = self.frame.data();
            let take = (count - written).min(frame.len() - self.offset);
            for i in 0..take {
                write(written + i, frame[self.offset + i] as f32 / 32768.0);
            }
            self.offset += take;
            written += take;
        }
        true
    }
}

#[no_mangle]
pub extern "C" fn winzapp_neteq_abi() -> u32 {
    1
}

#[no_mangle]
pub extern "C" fn winzapp_neteq_create(rate: u32) -> *mut Playout {
    catch_unwind(|| {
        Playout::new(rate as usize).map_or(std::ptr::null_mut(), |p| Box::into_raw(Box::new(p)))
    })
    .unwrap_or(std::ptr::null_mut())
}

/// All calls for a handle must be serialized by the caller, including destroy.
#[no_mangle]
pub unsafe extern "C" fn winzapp_neteq_insert(
    handle: *mut Playout,
    timestamp: u32,
    pcm: *const f32,
    count: u32,
) -> i32 {
    if handle.is_null() || pcm.is_null() || count == 0 || count > 5760 {
        return -1;
    }
    let state = &mut *handle;
    match catch_unwind(AssertUnwindSafe(|| {
        state.insert(timestamp, slice::from_raw_parts(pcm, count as usize))
    })) {
        Ok(true) => 0,
        _ => {
            state.failed = true;
            -1
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn winzapp_neteq_fill(
    handle: *mut Playout,
    pcm: *mut f32,
    count: u32,
) -> i32 {
    if handle.is_null() || pcm.is_null() || count == 0 || count > 96000 {
        return -1;
    }
    let state = &mut *handle;
    let output = slice::from_raw_parts_mut(pcm, count as usize);
    match catch_unwind(AssertUnwindSafe(|| state.fill(output))) {
        Ok(true) => 0,
        _ => {
            state.failed = true;
            output.fill(0.0);
            -1
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn winzapp_neteq_render(
    handle: *mut Playout,
    pcm: *mut f32,
    count: u32,
    stride: u32,
) -> i32 {
    if handle.is_null()
        || pcm.is_null()
        || count == 0
        || count > 96000
        || stride == 0
        || stride > 32
    {
        return -1;
    }
    let state = &mut *handle;
    match catch_unwind(AssertUnwindSafe(|| {
        state.render(count as usize, |i, v| *pcm.add(i * stride as usize) = v)
    })) {
        Ok(true) => 0,
        _ => {
            state.failed = true;
            for i in 0..count as usize {
                *pcm.add(i * stride as usize) = 0.0;
            }
            -1
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn winzapp_neteq_stats(handle: *const Playout, result: *mut u64) -> i32 {
    if handle.is_null() || result.is_null() {
        return -1;
    }
    let state = &*handle;
    catch_unwind(AssertUnwindSafe(|| {
        let stats = state.neteq.lifetime_statistics();
        let out = slice::from_raw_parts_mut(result, 4);
        out[0] = stats.concealed_samples;
        out[1] = stats.removed_samples_for_acceleration;
        out[2] = stats.inserted_samples_for_deceleration;
        out[3] = state.neteq.target_delay_ms().max(0) as u64;
    }))
    .map_or(-1, |_| 0)
}

#[no_mangle]
pub unsafe extern "C" fn winzapp_neteq_destroy(handle: *mut Playout) {
    if !handle.is_null() {
        drop(Box::from_raw(handle));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn tone(rate: usize, phase: usize, count: usize) -> Vec<f32> {
        (phase..phase + count)
            .map(|i| (i as f32 * 440.0 * std::f32::consts::TAU / rate as f32).sin() * 0.3)
            .collect()
    }
    #[test]
    fn device_periods_preserve_the_same_native_playout() {
        for rate in [8000, 16000, 32000, 48000] {
            let mut a = Playout::new(rate).unwrap();
            let mut b = Playout::new(rate).unwrap();
            for i in 0..4 {
                let pcm = tone(rate, i * rate * 60 / 1000, rate * 60 / 1000);
                assert!(a.insert((i * rate * 60 / 1000) as u32, &pcm));
                assert!(b.insert((i * rate * 60 / 1000) as u32, &pcm));
            }
            let mut whole = vec![0.0; rate / 10];
            let mut partial = vec![0.0; rate / 10];
            assert!(a.fill(&mut whole));
            for chunk in partial.chunks_mut(137) {
                assert!(b.fill(chunk));
            }
            assert_eq!(whole, partial);
            assert!(whole.iter().any(|v| v.abs() > 0.01));
        }
    }
    #[test]
    fn gap_uses_expand_and_returning_speech_uses_merge() {
        let rate = 16000;
        let mut p = Playout::new(rate).unwrap();
        let mut out = vec![0.0; 160];
        let mut merged = false;
        for tick in 0..120 {
            if tick % 6 == 0 && !(42..66).contains(&tick) {
                assert!(p.insert((tick * 160) as u32, &tone(rate, tick * 160, 960)));
            }
            assert!(p.fill(&mut out));
            merged |= p.neteq.last_operation() == neteq::Operation::Merge;
            assert!(out.iter().all(|v| v.is_finite() && v.abs() <= 1.0));
        }
        assert!(p.neteq.lifetime_statistics().concealed_samples > 0);
        assert!(merged, "speech recovery did not use Merge");
        assert!(out.iter().any(|v| v.abs() > 0.01));
    }
    #[test]
    fn bounds_and_silence_before_first_packet() {
        assert!(Playout::new(44100).is_none());
        let mut p = Playout::new(48000).unwrap();
        let mut out = vec![1.0; 137];
        assert!(p.fill(&mut out));
        assert_eq!(out, vec![0.0; 137]);
        assert!(!p.insert(0, &[]));
        assert!(!p.insert(0, &vec![0.0; 5761]));
    }
    #[test]
    fn burst_uses_accelerate_and_strided_render_leaves_other_channels_untouched() {
        let rate = 16000;
        let mut p = Playout::new(rate).unwrap();
        let mut out = vec![0.0; 160];
        let mut next = 0;
        for tick in 0..500 {
            if tick % 6 == 0 {
                let packets = if tick == 120 { 6 } else { 1 };
                for _ in 0..packets {
                    assert!(p.insert(next as u32, &tone(rate, next, 960)));
                    next += 960;
                }
            }
            assert!(p.fill(&mut out));
        }
        assert!(
            p.neteq
                .lifetime_statistics()
                .removed_samples_for_acceleration
                > 0,
            "{:?}",
            p.neteq.operations_and_state()
        );
        let mut interleaved = vec![0.75_f32; 160 * 8];
        let handle = &mut p as *mut Playout;
        assert_eq!(
            unsafe { winzapp_neteq_render(handle, interleaved.as_mut_ptr(), 160, 8) },
            0
        );
        assert!(interleaved
            .chunks(8)
            .all(|chunk| chunk[1..].iter().all(|v| *v == 0.75)));
        assert_eq!(
            unsafe { winzapp_neteq_render(handle, interleaved.as_mut_ptr(), 160, 0) },
            -1
        );
    }
}
