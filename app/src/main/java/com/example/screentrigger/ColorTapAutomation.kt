@file:Suppress("MissingPermission")

package com.example.screentrigger

/*
 * Usage / Deployment (Android 10+)
 * ------------------------------------------------------------
 * This file implements:
 * 1) ProjectionConsentActivity: requests MediaProjection permission once.
 * 2) ColorTapAccessibilityService: captures a centered region with ImageReader,
 *    scans pixels for configured target colors + tolerance, and dispatches
 *    randomized touch gestures through AccessibilityService.dispatchGesture().
 *
 * Build/deploy:
 * - Add this file to your Android app module.
 * - In AndroidManifest.xml:
 *   - Declare ProjectionConsentActivity.
 *   - Declare ColorTapAccessibilityService with BIND_ACCESSIBILITY_SERVICE,
 *     plus an accessibilityservice XML config.
 *   - Declare a foreground service type if you extract capture to a Service
 *     (not required in this single-file approach).
 * - Launch ProjectionConsentActivity once to grant capture permission.
 * - Enable the accessibility service in Settings > Accessibility.
 *
 * Required capabilities:
 * - MediaProjection user consent (screen capture)
 * - Accessibility service enabled by user (touch injection via dispatchGesture)
 * - Root is NOT required for this implementation.
 *
 * Parameter tuning:
 * - Edit constants in "=== CONFIGURATION ===" below.
 *
 * Runtime behavior:
 * - While service is connected and projection is granted, it continuously grabs
 *   latest frame from ImageReader, crops/scans a centered region, and when any
 *   pixel matches any target color within per-channel tolerance, it performs a
 *   humanized tap at TARGET_TOUCH_X/Y with randomized pre-press delay, hold,
 *   and cooldown.
 */

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Path
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.util.DisplayMetrics
import android.util.Log
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import kotlin.math.abs
import kotlin.random.Random

// === CONFIGURATION ===
private const val CAPTURE_WIDTH = 40
private const val CAPTURE_HEIGHT = 40
private const val COLOR_TOLERANCE = 10
private const val TARGET_TOUCH_X = 1080f
private const val TARGET_TOUCH_Y = 1920f
private const val CAPTURE_INTERVAL_MS = 12L // polling interval when no fresh frame

// Humanization timing (milliseconds)
private const val DELAY_BEFORE_PRESS_MIN = 10
private const val DELAY_BEFORE_PRESS_MAX = 50
private const val TOUCH_HOLD_MIN = 20
private const val TOUCH_HOLD_MAX = 80
private const val COOLDOWN_MIN = 500
private const val COOLDOWN_MAX = 1500

private const val TAG = "ColorTapAutomation"

private data class Rgb(val r: Int, val g: Int, val b: Int)

private val TARGET_COLORS = arrayOf(
    Rgb(222, 132, 255),
    Rgb(238, 143, 211),
    Rgb(253, 118, 255),
    Rgb(255, 150, 235),
)

/**
 * Launch this Activity once to obtain MediaProjection consent.
 * It stores the result in ProjectionGrantStore for the accessibility service.
 */
class ProjectionConsentActivity : Activity() {
    private lateinit var projectionManager: MediaProjectionManager

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        projectionManager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        startActivityForResult(projectionManager.createScreenCaptureIntent(), REQ_CAPTURE)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_CAPTURE && resultCode == RESULT_OK && data != null) {
            ProjectionGrantStore.resultCode = resultCode
            ProjectionGrantStore.dataIntent = data
            Log.i(TAG, "MediaProjection consent granted.")
        } else {
            Log.w(TAG, "MediaProjection consent denied.")
        }
        finish()
    }

    companion object {
        private const val REQ_CAPTURE = 1001
    }
}

/**
 * In-memory store for MediaProjection grant result.
 * For production persistence across process death, use a token handoff flow from
 * a foreground component after app relaunch.
 */
private object ProjectionGrantStore {
    @Volatile
    var resultCode: Int? = null

    @Volatile
    var dataIntent: Intent? = null
}

class ColorTapAccessibilityService : AccessibilityService() {
    private val mainHandler = Handler(Looper.getMainLooper())
    private lateinit var workerThread: HandlerThread
    private lateinit var workerHandler: Handler

    private var mediaProjection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var imageReader: ImageReader? = null

    @Volatile
    private var running = false

    @Volatile
    private var cooldownUntilMs = 0L

    private val random = Random.Default

    override fun onServiceConnected() {
        super.onServiceConnected()
        workerThread = HandlerThread("capture-worker", Thread.NORM_PRIORITY + 1)
        workerThread.start()
        workerHandler = Handler(workerThread.looper)

        if (!initProjectionAndPipeline()) {
            Log.e(TAG, "Projection pipeline init failed. Start ProjectionConsentActivity first.")
            return
        }

        running = true
        workerHandler.post(loopRunnable)
        Log.i(TAG, "ColorTapAccessibilityService started.")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit

    override fun onInterrupt() {
        stopEverything()
    }

    override fun onDestroy() {
        super.onDestroy()
        stopEverything()
    }

    private fun stopEverything() {
        running = false
        workerHandler.removeCallbacksAndMessages(null)
        releaseCapture()
        if (::workerThread.isInitialized) {
            workerThread.quitSafely()
        }
        Log.i(TAG, "ColorTapAccessibilityService stopped.")
    }

    private fun initProjectionAndPipeline(): Boolean {
        val resultCode = ProjectionGrantStore.resultCode ?: return false
        val dataIntent = ProjectionGrantStore.dataIntent ?: return false

        val projectionManager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        mediaProjection = projectionManager.getMediaProjection(resultCode, dataIntent)

        val metrics = DisplayMetrics()
        @Suppress("DEPRECATION")
        (getSystemService(Context.WINDOW_SERVICE) as WindowManager).defaultDisplay.getRealMetrics(metrics)

        imageReader = ImageReader.newInstance(
            metrics.widthPixels,
            metrics.heightPixels,
            android.graphics.PixelFormat.RGBA_8888,
            2,
        )

        virtualDisplay = mediaProjection?.createVirtualDisplay(
            "color-tap-display",
            metrics.widthPixels,
            metrics.heightPixels,
            metrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            imageReader?.surface,
            null,
            workerHandler,
        )

        mediaProjection?.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {
                Log.w(TAG, "MediaProjection stopped by system.")
                running = false
                releaseCapture()
            }
        }, mainHandler)

        return virtualDisplay != null
    }

    private fun releaseCapture() {
        try {
            virtualDisplay?.release()
        } catch (_: Throwable) {
        }
        virtualDisplay = null

        try {
            imageReader?.close()
        } catch (_: Throwable) {
        }
        imageReader = null

        try {
            mediaProjection?.stop()
        } catch (_: Throwable) {
        }
        mediaProjection = null
    }

    private val loopRunnable = object : Runnable {
        override fun run() {
            if (!running) return
            try {
                val now = System.currentTimeMillis()
                val image = imageReader?.acquireLatestImage()
                if (image != null) {
                    image.use {
                        if (now >= cooldownUntilMs && hasMatchingPixel(it)) {
                            val preDelay = randomInRange(DELAY_BEFORE_PRESS_MIN, DELAY_BEFORE_PRESS_MAX).toLong()
                            val hold = randomInRange(TOUCH_HOLD_MIN, TOUCH_HOLD_MAX).toLong()
                            val cooldown = randomInRange(COOLDOWN_MIN, COOLDOWN_MAX).toLong()

                            cooldownUntilMs = now + preDelay + hold + cooldown
                            workerHandler.postDelayed(
                                { dispatchTapHumanized(TARGET_TOUCH_X, TARGET_TOUCH_Y, hold) },
                                preDelay,
                            )
                        }
                    }
                }
            } catch (t: Throwable) {
                Log.e(TAG, "Capture loop error", t)
            } finally {
                workerHandler.postDelayed(this, CAPTURE_INTERVAL_MS)
            }
        }
    }

    private fun hasMatchingPixel(image: Image): Boolean {
        val width = image.width
        val height = image.height
        if (width <= 0 || height <= 0) return false

        val zoneW = CAPTURE_WIDTH.coerceAtMost(width)
        val zoneH = CAPTURE_HEIGHT.coerceAtMost(height)
        val startX = (width - zoneW) / 2
        val startY = (height - zoneH) / 2

        val plane = image.planes.firstOrNull() ?: return false
        val buffer = plane.buffer
        val rowStride = plane.rowStride
        val pixelStride = plane.pixelStride

        for (y in 0 until zoneH) {
            val rowOffset = (startY + y) * rowStride
            for (x in 0 until zoneW) {
                val offset = rowOffset + (startX + x) * pixelStride
                if (offset + 2 >= buffer.limit()) continue

                // MediaProjection RGBA_8888 buffer is little-endian in memory on most devices:
                // byte0=R, byte1=G, byte2=B, byte3=A.
                // If device-specific ordering differs, adjust mapping here.
                val r = buffer.get(offset).toInt() and 0xFF
                val g = buffer.get(offset + 1).toInt() and 0xFF
                val b = buffer.get(offset + 2).toInt() and 0xFF

                if (matchesAnyTarget(r, g, b)) return true
            }
        }
        return false
    }

    private fun matchesAnyTarget(r: Int, g: Int, b: Int): Boolean {
        for (target in TARGET_COLORS) {
            if (abs(r - target.r) <= COLOR_TOLERANCE &&
                abs(g - target.g) <= COLOR_TOLERANCE &&
                abs(b - target.b) <= COLOR_TOLERANCE
            ) {
                return true
            }
        }
        return false
    }

    private fun dispatchTapHumanized(x: Float, y: Float, holdMs: Long) {
        val path = Path().apply { moveTo(x, y) }
        val stroke = GestureDescription.StrokeDescription(path, 0L, holdMs.coerceAtLeast(1L))
        val gesture = GestureDescription.Builder().addStroke(stroke).build()

        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription?) {
                // Intentionally minimal logging for performance/noise.
            }

            override fun onCancelled(gestureDescription: GestureDescription?) {
                Log.w(TAG, "Tap gesture cancelled")
            }
        }, null)
    }

    private fun randomInRange(min: Int, max: Int): Int {
        if (max <= min) return min
        return random.nextInt(from = min, until = max + 1)
    }
}

private inline fun <T : AutoCloseable?, R> T.use(block: (T) -> R): R {
    var thrown: Throwable? = null
    try {
        return block(this)
    } catch (t: Throwable) {
        thrown = t
        throw t
    } finally {
        try {
            this?.close()
        } catch (closeError: Throwable) {
            if (thrown != null) thrown.addSuppressed(closeError) else throw closeError
        }
    }
}
