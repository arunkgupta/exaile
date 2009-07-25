# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.

import gtk, pango, gtk.gdk, gobject
from xlgui import guiutil, menu, plcolumns
from xlgui import rating
from xlgui.plcolumns import *
from xl import playlist, event, track, collection, xdg
from xl import settings, trackdb
from xl.nls import gettext as _
import copy, urllib
import logging
import os, os.path
import math
logger = logging.getLogger(__name__)

class Playlist(gtk.VBox):
    """
        Represents an xl.playlist.Playlist in the GUI

        If you want to add a possible column to the display of each playlist,
        just define a class for it in plcolumns.py and the rest will be done
        automatically
    """
    COLUMNS = plcolumns.COLUMNS
    column_by_display = {}
    for col in COLUMNS.values():
        column_by_display[col.display] = col

    default_columns = ['tracknumber', 'title', 'album', 'artist', '__length']
    menu_items = {}
    _is_drag_source = False
    
    __gsignals__ = {
        'playlist-content-changed': (gobject.SIGNAL_RUN_LAST, None, (bool,)),
    }
    def __init__(self, main, queue, pl):
        """
            Initializes the playlist

            @param pl: the playlist.Playlist instace to represent
        """
        gtk.VBox.__init__(self)

        self.exaile = main.controller.exaile

        self.main = main
        self.player = self.exaile.player
        self.queue = queue
        self.search_keyword = ''
        self.xml = main.xml
        self.dirty = False

        self.playlist = copy.copy(pl)
        self.playlist.ordered_tracks = pl.ordered_tracks[:]

        # see plcolumns.py for more information on the columns menu
        if not Playlist.menu_items:
            plcolumns.setup_menu(self.xml.get_widget('columns_menu_menu'),
                Playlist.menu_items)

        self._setup_tree()
        self._setup_col_menus()
        self._setup_columns()
        self._setup_events()
        self._set_tracks(self.playlist.get_tracks())

        self.menu = menu.PlaylistMenu(self, main.playlist_manager)
        self.menu.connect('rating-set', self.set_rating)
        self.menu.connect('remove-items', lambda *e:
            self.remove_selected_tracks())
        self.menu.connect('queue-items', lambda *e:
            self.queue_selected_tracks())

        self.show_all()

        # watch the playlist for changes
        event.add_callback(self.on_add_tracks, 'tracks_added', self.playlist)
        event.add_callback(self.on_remove_tracks, 'tracks_removed',
            self.playlist)

    def queue_selected_tracks(self):
        """
            Toggles queue of selected tracks
        """
        tracks = self.get_selected_tracks()

        queue_tracks = self.queue.ordered_tracks
        for track in tracks:
            if track in queue_tracks:
                queue_tracks.remove(track)
            else:
                queue_tracks.append(track)

        self.main.update_track_counts()
        self.list.queue_draw()

    def set_rating(self, widget, rating):
        tracks = self.get_selected_tracks()
        steps = settings.get_option('miscellaneous/rating_steps', 5)
        r = float((100.0*rating)/steps)
        for track in tracks:
            track['__rating'] = r
        event.log_event('rating_changed', self, r)

    def _setup_col_menus(self):
        """
            Sets up the column menus (IE, View->Column->Track, etc)
        """
        self.resizable_cols = self.xml.get_widget('col_resizable_item')
        self.not_resizable_cols = \
            self.xml.get_widget('col_not_resizable_item')
        if not self.resizable_cols and not self.not_resizable_cols:
            return # potentially dangerous if someone breaks the gladefile...
        self.resizable_cols.set_active(
                settings.get_option('gui/resizable_cols', False))
        self.not_resizable_cols.set_active(not \
            settings.get_option('gui/resizable_cols', False))
        self.resizable_cols.connect('activate', self.activate_cols_resizable)
        self.not_resizable_cols.connect('activate',
            self.activate_cols_resizable)

        column_ids = None
        if settings.get_option('gui/trackslist_defaults_set', False):
            column_ids = set()
            ids = settings.get_option("gui/columns", [])
            # Don't add invalid columns.
            all_ids = frozenset(self.COLUMNS.keys())
            for id in ids:
                if id in all_ids:
                    column_ids.add(id)

        if not column_ids:
            # Use default.
            ids = self.default_columns
            settings.set_option('gui/trackslist_defaults_set', True)
            settings.set_option('gui/columns', ids)
            column_ids = frozenset(ids)

        for col_struct in self.COLUMNS.values():
            try:
                menu = Playlist.menu_items[col_struct.id]
            except KeyError:
                logger.warning("No such column: %s" % col_struct.id)
                continue

            menu.set_active(col_struct.id in column_ids)
            menu.connect('activate', self.change_column_settings,
                ('gui/columns', col_struct))

    def search(self, keyword):
        """
            Filter the playlist with a keyword
        """
        tracks = self.playlist.filter(keyword)
        self._set_tracks(tracks)
        self.search_keyword = keyword

    def change_column_settings(self, item, data):
        """
            Changes column view settings
        """
        pref, col_struct = data
        id = col_struct.id

        column_ids = list(settings.get_option(pref, []))
        if item.get_active():
            if id not in column_ids:
                logger.info(_("adding %(column_id)s column to %(preference)s") %
                    {'column_id' : id, 'preference' : pref})
                column_ids.append(id)
        else:
            if col_struct.id in column_ids:
                logger.info(_("removing %(column_id)s column from %(preference)s") %
                    {'column_id' : id, 'preference' : pref})
                column_ids.remove(id)
        settings.set_option(pref, column_ids)

        for i in range(0, self.main.playlist_notebook.get_n_pages()):
            page = self.main.playlist_notebook.get_nth_page(i)
            page.update_col_settings()

    def activate_cols_resizable(self, widget, event=None):
        """
            Called when the user chooses whether or not columns can be
            resizable
        """
        if 'not' in widget.name:
            resizable = False
        else:
            resizable = True

        settings.set_option('gui/resizable_cols', resizable)
        for i in range(0, self.main.playlist_notebook.get_n_pages()):
            page = self.main.playlist_notebook.get_nth_page(i)
            page.update_col_settings()

    def update_col_settings(self):
        """
            Updates the settings for a specific column
        """
        selection = self.list.get_selection()
        info = selection.get_selected_rows()

        self.list.disconnect(self.changed_id)
        columns = self.list.get_columns()
        for col in columns:
            self.list.remove_column(col)

        self._setup_columns()
        self._set_tracks(self.playlist.get_tracks())
        self.list.queue_draw()

        if info:
            paths = info[1]
            if paths:
                if paths[0]:
                    iter = self.model.get_iter(paths[0])
                    track = self.model.get_value(iter, 0)
                    index = self.playlist.index(track)
                    self.list.scroll_to_cell(index)
                    self.list.set_cursor(index)
                
                for path in paths:
                    selection.select_path(path)

    def on_remove_tracks(self, type, playlist, info):
        """
            Called when someone removes tracks from the contained playlist
        """
        self._set_tracks(playlist.get_tracks())
        self.reorder_songs()
        self.main.update_track_counts()
        self.set_dirty()

    def on_add_tracks(self, type, playlist, tracks):
        """
            Called when someone adds tracks to the contained playlist
        """
        for track in tracks:
            self._append_track(track)
        self.main.update_track_counts()
        
        if tracks and settings.get_option("gui/scroll_when_appending_tracks", False):
            self.list.scroll_to_cell(self.playlist.index(tracks[-1]))
            #self.list.scroll_to_cell(self.playlist.index(tracks[0]))
        self.set_dirty()

    def _set_tracks(self, tracks):
        """
            Sets the tracks that this playlist should display
        """
        self.model.clear()

        for track in tracks:
            self._append_track(track)

        self.list.set_model(self.model)
        self.main.update_track_counts()

        #Whenever we reset the model of the list
        #we need to mark the search column again
        self._set_search_column()

    def _set_search_column(self):
        count = 3
        search_column = settings.get_option("gui/search_column", "Title")
        for col in self.list.get_columns():
            if col.get_title() == search_column:
                self.list.set_search_column(count)
            count = count + 1

    def _get_ar(self, song):
        """
            Creates the array to be added to the model in the correct order
        """
        ar = [song, None, None]
        for field in self.append_map:
            try:
                if isinstance(song[field], basestring):
                    raise TypeError
                value = " / ".join(song[field])
            except TypeError:
                value = song[field]
            if value is None: value = ''

            ar.append(value)
        return ar

    def _append_track(self, track):
        """
            Adds a track to this view
        """
        ar = self._get_ar(track)
        self.model.append(ar)

    def get_selected_track(self):
        """
            Returns the currently selected track
        """
        tracks = self.get_selected_tracks()
        if not tracks: return None
        else: return tracks[0]

    def get_selected_tracks(self):
        """
            Gets the selected tracks in the tree view
        """
        selection = self.list.get_selection()
        (model, paths) = selection.get_selected_rows()
        songs = []
        for path in paths:
            iter = self.model.get_iter(path)
            song = self.model.get_value(iter, 0)
            songs.append(song)

        return songs

    def update_iter(self, iter, song):
        """
            Updates the track at "iter"
        """
        ar = self._get_ar(song)
        self.model.insert_after(iter, ar)
        self.model.remove(iter)

    def refresh_row(self, song):
        """
            Refreshes the text for the specified row
        """
        selection = self.list.get_selection()
        model, paths = selection.get_selected_rows()
        iter = self.model.get_iter_first()
        if not iter: return
        while True:
            check = self.model.get_value(iter, 0)
            if not check: break
            if check == song or check.get_loc() == song.get_loc():
                self.update_iter(iter, song)
                break
            iter = self.model.iter_next(iter)
            if not iter: break

        self.list.queue_draw()

    def on_row_activated(self, *e):
        """
            Called when the user double clicks on a track
        """
        track = self.get_selected_track()
        if not track: return

        index = self.playlist.index(track)
        self.playlist.set_current_pos(index)
        self.queue.play(track=track)
        self.queue.set_current_playlist(self.playlist)

    def on_closing(self):
        """
            Called by the NotebookTab when this playlist
            is about to be closed.  Handles such things
            as confirming a close on a modified playlist

            @return: True if we should continue to close,
                False otherwise
        """
        # Before closing check whether the playlist
        # changed, and if it did give the user an option to do something
        if self.dirty:
            try:
                current_tracks = self.playlist.get_tracks()
                original_tracks = self.main.playlist_manager.get_playlist \
                    (self.playlist.get_name()).get_tracks()
                dirty = False
                if len(current_tracks) != len(original_tracks):
                    dirty = True
                else:
                    for i in range(0, len(original_tracks)):
                        o_track = original_tracks[i]
                        c_track = current_tracks[i]
                        if o_track != c_track:
                            dirty = True
                            break

                if dirty == True and self.playlist.get_playlist_kind() == 'custom':
                    dialog = ConfirmCloseDialog(self.playlist.get_name())
                    result = dialog.run()
                    if result == 110:
                        # Save the playlist then close
                        self.main.playlist_manager.save_playlist(
                            self.playlist, overwrite = True)
                        return True
                    elif result == gtk.RESPONSE_CANCEL:
                        return False
            except ValueError:
                # Usually means that it was a smart playlist
                pass
        return True

    def button_press(self, button, event):
        """
            Called when the user clicks on the playlist
        """
        if event.button == 3:
            tab = self.main.get_current_tab()
            (x, y) = event.get_coords()
            path = self.list.get_path_at_pos(int(x), int(y))
            if path:
                self.menu.popup(event)
            else:
                tab.menu.popup(None, None, None, event.button, event.time)
            if len(self.get_selected_tracks()) > 1: return True
        return False

    def _setup_events(self):
        self.list.connect('key_release_event', self.key_released)
        self.list.connect('button-release-event', self.update_rating)

    def key_released(self, widget, event):
        if event.keyval == gtk.keysyms.Delete:
            self.remove_selected_tracks()
        elif event.keyval == gtk.keysyms.Left:
            # Modifying current position
            self.player.scroll(-10)
            self.main.progress_bar.timer_update() # Needed to evade progressbar lag
        elif event.keyval == gtk.keysyms.Right:
            # Modifying current position
            self.player.scroll(10)
            self.main.progress_bar.timer_update() # Needed to evade progressbar lag

    def _setup_tree(self):
        """
            Sets up the TreeView for this Playlist
        """
        self.list = guiutil.DragTreeView(self)
        self.list.set_rules_hint(True)
        self.list.set_enable_search(True)
        self.list.connect('row-activated', self.on_row_activated)

        self.scroll = gtk.ScrolledWindow()
        self.scroll.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
        self.scroll.add(self.list)
        self.pack_start(self.scroll, True, True)
        self.scroll.show_all()

        selection = self.list.get_selection()
        selection.set_mode(gtk.SELECTION_MULTIPLE)

        window = gtk.Window()
        img = window.render_icon('gtk-media-play',
            gtk.ICON_SIZE_SMALL_TOOLBAR)
        self.playimg = img.scale_simple(18, 18,
            gtk.gdk.INTERP_BILINEAR)
        img = window.render_icon('gtk-media-pause',
            gtk.ICON_SIZE_SMALL_TOOLBAR)
        self.pauseimg = img.scale_simple(18, 18,
            gtk.gdk.INTERP_BILINEAR)

    def column_changed(self, *e):
        """
            Called when columns are reordered
        """
        self.list.disconnect(self.changed_id)
        cols = []
        for col in self.list.get_columns():
            cols.append(self.column_by_display[col.get_title()].id)
            self.list.remove_column(col)

        settings.set_option('gui/columns', cols)
        self._setup_columns()
        self._set_tracks(self.playlist.get_tracks())
    
    def set_dirty(self):
        self.dirty = True
        self.emit('playlist-content-changed', self.dirty)
    
    def unset_dirty(self):
        self.dirty = False
        self.emit('playlist-content-changed', self.dirty)

    def drag_data_received(self, tv, context, x, y, selection, info, etime):
        """
            Called when data is recieved
        """
        if self.playlist.ordered_tracks:
            curtrack = self.playlist.get_current()
        else:
            curtrack = None

        # Remove callbacks so they are not fired when we perform actions
        event.remove_callback(self.on_add_tracks, 'tracks_added', self.playlist)
        event.remove_callback(self.on_remove_tracks, 'tracks_removed',
            self.playlist)
        # Make sure the callbacks actually get removed before proceeding
        event.wait_for_pending_events()

        self.list.unset_rows_drag_dest()
        self.list.drag_dest_set(gtk.DEST_DEFAULT_ALL,
            self.list.targets,
            gtk.gdk.ACTION_COPY|gtk.gdk.ACTION_MOVE)

        locs = list(selection.get_uris())
        count = 0

        if context.action != gtk.gdk.ACTION_MOVE:
            pass

        drop_info = tv.get_dest_row_at_pos(x, y)
        if drop_info:
            path, position = drop_info
            iter = self.model.get_iter(path)
            if (position == gtk.TREE_VIEW_DROP_BEFORE or
                position == gtk.TREE_VIEW_DROP_INTO_OR_BEFORE):
                first = False
            else:
                first = True

        current_tracks = self.playlist.get_tracks()
        (tracks, playlists) = self.list.get_drag_data(locs)

        tracks = sort_tracks(tracks)

        # Determine what to do with the tracks
        # by default we load all tracks.
        # TODO: should we load tracks we find in the collection from there??
        for track in tracks:
            if not Playlist._is_drag_source and track in current_tracks:
                continue
            if not drop_info:
                self._append_track(track)
            else:
                if not first:
                    first = True
                    ar = self._get_ar(track)
                    if self.model.iter_is_valid(iter):
                        iter = self.model.insert_before(iter, ar)
                    else:
                        iter = self.model.append(ar)
                else:
                    ar = self._get_ar(track)
                    path = self.model.get_path(iter)
                    if self.model.iter_is_valid(iter):
                        iter = self.model.insert_after(iter, ar)
                    else:
                        iter = self.model.append(ar)

        Playlist._is_drag_source = False
        if context.action == gtk.gdk.ACTION_MOVE:
            # On a move action the second True makes the
            # drag_data_delete function called
            context.finish(True, True, etime)
        else:
            context.finish(True, False, etime)

        # iterates through the list and adds any tracks that are
        # not in the playlist to the current playlist
        current_tracks = self.playlist.get_tracks()
        iter = self.model.get_iter_first()
        if not iter:
            # Do we need to reactivate the callbacks when this happens?
            gobject.idle_add(self.add_track_callbacks)
            return
        while True:
            track = self.model.get_value(iter, 0)
            if not track in current_tracks:
                self.playlist.add_tracks((track,))
            iter = self.model.iter_next(iter)
            if not iter: break

        # Re add all of the tracks so that they
        # become ordered
        iter = self.model.get_iter_first()
        if not iter:
            gobject.idle_add(self.add_track_callbacks)
            return

        self.playlist.ordered_tracks = []
        while True:
            track = self.model.get_value(iter, 0)
            self.playlist.ordered_tracks.append(track)
            iter = self.model.iter_next(iter)
            if not iter: break

        if tracks:
            self.set_dirty()

        gobject.idle_add(self.add_track_callbacks)
        self.main.update_track_counts()

        if curtrack is not None:
            index = self.playlist.index(curtrack)
            self.playlist.set_current_pos(index)

    def add_track_callbacks(self):
        """
            Adds callbacks for added and removed tracks.
        """
        event.add_callback(self.on_add_tracks, 'tracks_added', self.playlist)
        event.add_callback(self.on_remove_tracks, 'tracks_removed',
            self.playlist)

    def remove_selected_tracks(self):
        sel = self.list.get_selection()
        (model, paths) = sel.get_selected_rows()
        # Since we want to modify the model we make references to it
        # This allows us to remove rows without it messing up
        rows = []
        for path in paths:
            rows.append(gtk.TreeRowReference(model, path))
        for row in rows:
            iter = self.model.get_iter(row.get_path())
            #Also update the playlist we have
            track = self.model.get_value(iter, 0)
            self.playlist.remove(self.playlist.index(track))
            self.model.remove(iter)

    def drag_data_delete(self, tv, context):
        """
            Called after a drag data operation is complete
            and we want to delete the source data
        """
        if context.drag_drop_succeeded():
            self.remove_selected_tracks()

    def drag_get_data(self, treeview, context, selection, target_id, etime):
        """
            Called when a drag source wants data for this drag operation
        """
        Playlist._is_drag_source = True
        loc = []
        delete = []
        sel = self.list.get_selection()
        (model, paths) = sel.get_selected_rows()
        for path in paths:
            iter = self.model.get_iter(path)
            song = self.model.get_value(iter, 0)

            uri = urllib.quote(song.get_loc().encode("utf-8"))
            guiutil.DragTreeView.dragged_data[song.get_loc()] = song
            loc.append(uri)

        selection.set_uris(loc)

    def setup_model(self, map):
        """
            Gets the array to build the two models
        """
        ar = [object, gtk.gdk.Pixbuf, gtk.gdk.Pixbuf]

        for item in map:
            ar.append(str)

        self.model = gtk.ListStore(*ar)
        self.model_blank = gtk.ListStore(*ar)
        self.list.set_model(self.model)

    def _setup_columns(self):
        """
            Sets up the columns for this table
        """

        self._col_count = 0

        col_ids = settings.get_option("gui/columns", [])
        search_column = settings.get_option("gui/search_column", "Title")

        # make sure all the entries are good
        if col_ids:
            cols = []
            for col in col_ids:
                if col in self.COLUMNS:
                    cols.append(col)
            col_ids = cols

        if not col_ids:
            col_ids = self.default_columns

        self.append_map = col_ids
        self.setup_model(col_ids)

        count = 3
        first_col = True

        for col in col_ids:
            column = self.COLUMNS[col](self)
            cellr = column.renderer()

            if first_col:
                first_col = False
                pb = gtk.CellRendererPixbuf()
                pb.set_fixed_size(20, 20)
                pb.set_property('xalign', 0.0)
                stop_pb = gtk.CellRendererPixbuf()
                stop_pb.set_fixed_size(12, 12)
                col = gtk.TreeViewColumn(column.display)
                col.pack_start(pb, False)
                col.pack_start(stop_pb, False)
                col.pack_start(cellr, True)
                col.set_attributes(cellr, text=count)
                col.set_attributes(pb, pixbuf=1)
                col.set_attributes(stop_pb, pixbuf=2)
                col.set_cell_data_func(pb, self.icon_data_func)
                col.set_cell_data_func(stop_pb, self.stop_icon_data_func)
            else:
                col = gtk.TreeViewColumn(column.display, cellr, text=count)

            col.set_cell_data_func(cellr, column.data_func)
            column.set_properties(col, cellr)

            setting_name = "gui/col_width_%s" % column.id
            width = settings.get_option(setting_name,
                column.size)
            col.set_fixed_width(int(width))

            resizable = settings.get_option('gui/resizable_cols',
                False)

            col.connect('clicked', self.set_sort_by)
            col.connect('notify::width', self.set_column_width)
            col.set_clickable(True)
            col.set_reorderable(True)
            col.set_resizable(False)
            col.set_sort_indicator(False)

            if not resizable:
                if column.id in ('title', 'artist', 'album', '__loc', 'genre'):
                    if column.id != 'genre':
                        col.set_expand(True)
                        col.set_fixed_width(1)
                    else:
                        col.set_fixed_width(80)
                    col.set_sizing(gtk.TREE_VIEW_COLUMN_FIXED)
                    cellr.set_property('ellipsize', pango.ELLIPSIZE_END)
                else:
                    col.set_sizing(gtk.TREE_VIEW_COLUMN_AUTOSIZE)
            else:
                col.set_resizable(True)
                col.set_sizing(gtk.TREE_VIEW_COLUMN_FIXED)

            # Update which column to search for when columns are changed
            if column.display == search_column:
                self.list.set_search_column(count)
            col.set_widget(gtk.Label(column.display))
            col.get_widget().show()
            self.list.append_column(col)
            col.get_widget().get_ancestor(gtk.Button).connect('button_press_event',
                self.press_header)
            count = count + 1
        self.changed_id = self.list.connect('columns-changed', self.column_changed)

    def set_column_width(self, col, *e):
        """
            Called when the user resizes a column
        """
        col_struct = self.column_by_display[col.get_title()]
        name = 'gui/col_width_%s' % col_struct.id
        w = col.get_width()
        if w != settings.get_option(name, -1):
            settings.set_option(name, w)

    # sort functions courtesy of listen (http://listengnome.free.fr), which
    # are in turn, courtesy of quodlibet.
    def set_sort_by(self, column):
        """
            Sets the sort column
        """
        title = column.get_title()
        for col in self.list.get_columns():
            if title == col.get_title():
                order = column.get_sort_order()
                if order == gtk.SORT_ASCENDING:
                    order = gtk.SORT_DESCENDING
                else:
                    order = gtk.SORT_ASCENDING
                col.set_sort_indicator(True)
                col.set_sort_order(order)
            else:
                col.set_sort_indicator(False)

        tracks = self.reorder_songs()
        self._set_tracks(tracks)

        if not self.playlist.ordered_tracks: return
        try:
            curtrack = \
                self.playlist.ordered_tracks[self.playlist.get_current_pos()]
        except IndexError:
            curtrack = self.playlist.ordered_tracks[0]
        self.playlist.ordered_tracks = tracks
        index = self.playlist.index(curtrack)
        self.playlist.set_current_pos(index)

    def reorder_songs(self):
        """
            Resorts all songs
        """
        attr, reverse = self.get_sort_by()

        songs = self.playlist.search(self.search_keyword,
            (attr, 'artist', 'date', 'album', 'discnumber', 'tracknumber', 'title'))

        if reverse:
            songs.reverse()
        return songs

    def get_sort_by(self):
        """
            Gets the sort order
        """
        for col in self.list.get_columns():
            if col.get_sort_indicator():
                return (self.column_by_display[col.get_title()].id,
                    col.get_sort_order() == gtk.SORT_DESCENDING)
        return 'tracknumber', False

    def icon_data_func(self, col, cell, model, iter):
        """
            Sets track status (playing/paused/queued) icon
        """

        item = model.get_value(iter, 0)
        image = None

        if item == self.player.current:
            if self.player.is_playing():
                image = self.playimg
            elif self.player.is_paused():
                image = self.pauseimg

        # queued items
        elif item in self.queue.ordered_tracks:
            index = self.queue.ordered_tracks.index(item)
            image = guiutil.get_text_icon(self.main.window,
                str(index + 1), 18, 18)

        cell.set_property('pixbuf', image)


    def stop_icon_data_func(self, col, cell, model, iter):
        """
            Sets "stop after this" icon
        """

        item = model.get_value(iter, 0)
        image = None

        window = gtk.Window()
        if item == self.queue.stop_track:
            image = window.render_icon('gtk-stop',
                gtk.ICON_SIZE_MENU)
            image = image.scale_simple(12, 12, gtk.gdk.INTERP_BILINEAR)

        cell.set_property('pixbuf', image)


    def set_cell_weight(self, cell, item):
        """
            Sets a CellRendererText's "weight" property according to whether
            `item` is the currently playing track.
        """
        if item == self.player.current:
            weight = pango.WEIGHT_HEAVY
        else:
            weight = pango.WEIGHT_NORMAL
        cell.set_property('weight', weight)

    def press_header(self, widget, event):
        if event.button != 3:
            return False
        menu = self.xml.get_widget('columns_menu_menu')
        menu.popup(None, None, None, event.button, event.time)
        return True

    def _print_playlist(self, banner = ''):
        """
            Debug - prints the current playlist to stdout
        """
        print banner
        tracks = self.playlist.get_tracks()
        for track in tracks:
            print track.get_loc()
        print '---Done printing playlist'

    def update_rating(self, w, e):
        """
            Called when the user clicks on the playlist. If the user
            clicked on rating column the rating of the selected track
            is updated.
        """
        rating_col_width = 0
        left_edge = 0
        steps = settings.get_option("miscellaneous/rating_steps", 5)
        icon_size = rating._rating_width / steps
        cols = self.list.get_columns()
        i = 0
        #calculate rating column size and position
        for col in self.append_map:
            gui_col = self.list.get_column(i)
            if col == "__rating":
                rating_col_width = gui_col.get_width()
                break
            else:
                left_edge = left_edge + gui_col.get_width()
            i = i + 1

        (x, y) = e.get_coords()
        #check if the click is within rating column and on a list entry
        if self.list.get_path_at_pos(int(x), int(y)) \
            and left_edge < x < left_edge + rating_col_width:
                track = self.get_selected_track()
                leftpadding = (rating_col_width - rating._rating_width) / 2
                i = int(math.ceil((x-left_edge-leftpadding)/icon_size))
                new_rating = float((100*i)/steps)
                if track['__rating'] == new_rating:
                    track['__rating'] = 0.0
                else:
                    track['__rating'] = new_rating
                if hasattr(w, 'queue_draw'):
                    w.queue_draw()
                event.log_event('rating_changed', self, i)

def sort_tracks(tracks):
    from xlgui import main

    pl = main.get_selected_playlist()
    column, descending = pl.get_sort_by()
    
    if column != 'tracknumber':
        tracks.sort(key=lambda track: track.sort_param(column),
            reverse=descending)
    else:
        tracks = trackdb.sort_tracks(
            ('artist', 'date', 'album', 'discnumber', 'tracknumber'),
            tracks)
        if descending: tracks.reverse()

    return tracks

class ConfirmCloseDialog(gtk.MessageDialog):
    """
        Shows the dialog to confirm closing of the playlist
    """
    def __init__(self, document_name):
        """
            Initializes the dialog
        """
        gtk.MessageDialog.__init__(self, type = gtk.MESSAGE_WARNING)

        self.set_title(_('Close %s' % document_name))
        self.set_markup(_('<b>Save changes to %s before closing?</b>') % document_name)
        self.format_secondary_text(_('Your changes will be lost if you don\'t save them'))

        self.add_buttons(_('Close Without Saving'), 100, gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL,
                        gtk.STOCK_SAVE, 110)

    def run(self):
        self.show_all()
        response = gtk.Dialog.run(self)
        self.hide()
        return response
