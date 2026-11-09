package io.github.orimovo.curslink

import android.content.ComponentName
import android.content.Context
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService
import io.github.orimovo.curslink.core.LinkState

/** A Quick Settings tile: on while Curslink is linked, tap to pause or resume. */
class PauseTile : TileService() {
    companion object {
        fun refresh(context: Context) {
            runCatching { requestListeningState(context, ComponentName(context, PauseTile::class.java)) }
        }
    }

    private val listener = { show() }

    override fun onStartListening() {
        super.onStartListening()
        LinkService.listen(listener)
        show()
    }

    override fun onStopListening() {
        LinkService.unlisten(listener)
        super.onStopListening()
    }

    override fun onClick() {
        super.onClick()
        val prefs = Prefs(this)
        if (prefs.pairing == null) return
        LinkService.setPaused(this, !prefs.paused)
        show()
    }

    private fun show() {
        val tile = qsTile ?: return
        val prefs = Prefs(this)
        val (state, _) = LinkService.state
        val sub: String
        tile.state = when {
            prefs.pairing == null -> { sub = "Not paired"; Tile.STATE_UNAVAILABLE }
            prefs.paused -> { sub = "Paused"; Tile.STATE_INACTIVE }
            state == LinkState.PAUSED -> { sub = "Paused for a call"; Tile.STATE_INACTIVE }
            state == LinkState.CONNECTED -> { sub = "On"; Tile.STATE_ACTIVE }
            else -> { sub = "Looking…"; Tile.STATE_ACTIVE }
        }
        tile.label = getString(R.string.app_name)
        if (Build.VERSION.SDK_INT >= 29) tile.subtitle = sub
        tile.updateTile()
    }
}
