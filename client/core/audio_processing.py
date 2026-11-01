import logging
import numpy as np

def apply_noise_gate(audio_bytes: bytes, rate: int, channels: int, threshold_db: float = -40.0, attenuation: float = 0.1, *, strict: bool = False) -> bytes:
    """Legacy callers get the original audio back on error; strict re-raises.

    The neural fallback path needs to know when the gate did not run at all,
    so it passes strict=True and handles the announcement itself."""

    """
    Applies an Automatic Gain Control (AGC) and Dynamic Noise Gate filter
    to raw 16-bit PCM audio bytes using NumPy.
    This attenuates background noise during silences (noise gate) and
    levels/compresses the voice volume during speech (AGC), producing
    a clean, WebRTC-like compressed voice sound.
    """
    try:
        # Load raw bytes as 16-bit signed PCM samples
        samples = np.frombuffer(audio_bytes, dtype=np.int16).copy()
        
        # 20ms block size
        block_size = int(rate * 0.02) * channels
        if block_size <= 0:
            return audio_bytes
            
        num_samples = len(samples)
        
        # Compute RMS for all blocks first to estimate noise floor
        rms_values = []
        for i in range(0, num_samples, block_size):
            block = samples[i:i+block_size]
            if len(block) == 0:
                continue
            rms = np.sqrt(np.mean(block.astype(np.float32) ** 2))
            rms_values.append(rms)

        if not rms_values:
            return audio_bytes

        # Use the 15th percentile of RMS to find the background noise floor (pauses)
        noise_floor = float(np.percentile(rms_values, 15))
        
        # Threshold is 2.5x the noise floor, bounded between -45dB (180) and -22dB (2600)
        threshold = max(180.0, min(2600.0, noise_floor * 2.5))
        
        logging.info("[audio_processing] Dynamic AGC + Noise Gate: noise floor RMS = %.2f, threshold = %.2f", noise_floor, threshold)
        
        # Envelope parameters for gain smoothing (faster attack/release response)
        gain = 1.0
        attack = 0.35
        release = 0.15
        
        for i in range(0, num_samples, block_size):
            block = samples[i:i+block_size]
            if len(block) == 0:
                continue
            
            rms = rms_values[i // block_size] if (i // block_size) < len(rms_values) else np.sqrt(np.mean(block.astype(np.float32) ** 2))
            
            # Determine target gain factor
            if rms >= threshold:
                # Speech detected: Auto Gain Control to target 2000 RMS (comfortable volume)
                target_gain = 2000.0 / (rms + 1e-5)
                # Bound target gain between 0.5 (compression) and 2.5 (boosting)
                target_gain = max(0.5, min(2.5, target_gain))
            else:
                # Silence/noise detected: attenuate
                target_gain = attenuation
            
            # Smooth the gain transition (prevents audio clicking)
            if target_gain > gain:
                gain = gain + attack * (target_gain - gain)
            else:
                gain = gain + release * (target_gain - gain)
                
            # Apply smoothed gain to the block and prevent clipping
            # 16-bit PCM limit is [-32768, 32767]
            processed_block = np.clip(block.astype(np.float32) * gain, -32768.0, 32767.0)
            samples[i:i+block_size] = processed_block.astype(np.int16)
            
        return samples.tobytes()
    except Exception as e:
        logging.error("[audio_processing] Error applying dynamic AGC + Noise Gate: %s", e)
        if strict:
            raise
        return audio_bytes
