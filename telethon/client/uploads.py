import hashlib
import io
import os
import pathlib
import re
from io import BytesIO

from .buttons import ButtonMethods
from .messageparse import MessageParseMethods
from .users import UserMethods
from .. import utils, helpers
from ..tl import types, functions, custom

try:
    import PIL
    import PIL.Image
except ImportError:
    PIL = None


class _CacheType:
    """Like functools.partial but pretends to be the wrapped class."""
    def __init__(self, cls):
        self._cls = cls

    def __call__(self, *args, **kwargs):
        return self._cls(*args, file_reference=b'', **kwargs)

    def __eq__(self, other):
        return self._cls == other


def _resize_photo_if_needed(
        file, is_image, width=1280, height=1280, background=(255, 255, 255)):

    # https://github.com/telegramdesktop/tdesktop/blob/12905f0dcb9d513378e7db11989455a1b764ef75/Telegram/SourceFiles/boxes/photo_crop_box.cpp#L254
    if (not is_image
            or PIL is None
            or (isinstance(file, io.IOBase) and not file.seekable())):
        return file

    before = file.tell() if isinstance(file, io.IOBase) else 0
    if isinstance(file, bytes):
        file = io.BytesIO(file)

    try:
        # Don't use a `with` block for `image`, or `file` would be closed.
        # See https://github.com/LonamiWebs/Telethon/issues/1121 for more.
        image = PIL.Image.open(file)
        if image.width <= width and image.height <= height:
            return file

        image.thumbnail((width, height), PIL.Image.ANTIALIAS)

        alpha_index = image.mode.find('A')
        if alpha_index == -1:
            # If the image mode doesn't have alpha
            # channel then don't bother masking it away.
            result = image
        else:
            # We could save the resized image with the original format, but
            # JPEG often compresses better -> smaller size -> faster upload
            # We need to mask away the alpha channel ([3]), since otherwise
            # IOError is raised when trying to save alpha channels in JPEG.
            result = PIL.Image.new('RGB', image.size, background)
            result.paste(image, mask=image.split()[alpha_index])

        buffer = io.BytesIO()
        result.save(buffer, 'JPEG')
        buffer.seek(0)
        return buffer

    except IOError:
        return file
    finally:
        if before is not None:
            file.seek(before, io.SEEK_SET)


class UploadMethods(ButtonMethods, MessageParseMethods, UserMethods):

    # region Public methods

    async def send_file(
            self, entity, file, *, caption=None, force_document=False,
            progress_callback=None, reply_to=None, attributes=None,
            thumb=None, allow_cache=True, parse_mode=(),
            voice_note=False, video_note=False, buttons=None, silent=None,
            supports_streaming=False, **kwargs):
        """
        Sends a file to the specified entity.

        Args:
            entity (`entity`):
                Who will receive the file.

            file (`str` | `bytes` | `file` | `media`):
                The file to send, which can be one of:

                * A local file path to an in-disk file. The file name
                  will be the path's base name.

                * A `bytes` byte array with the file's data to send
                  (for example, by using ``text.encode('utf-8')``).
                  A default file name will be used.

                * A bytes `io.IOBase` stream over the file to send
                  (for example, by using ``open(file, 'rb')``).
                  Its ``.name`` property will be used for the file name,
                  or a default if it doesn't have one.

                * An external URL to a file over the internet. This will
                  send the file as "external" media, and Telegram is the
                  one that will fetch the media and send it.

                * A Bot API-like ``file_id``. You can convert previously
                  sent media to file IDs for later reusing with
                  `telethon.utils.pack_bot_file_id`.

                * A handle to an existing file (for example, if you sent a
                  message with media before, you can use its ``message.media``
                  as a file here).

                * A handle to an uploaded file (from `upload_file`).

                To send an album, you should provide a list in this parameter.

                If a list or similar is provided, the files in it will be
                sent as an album in the order in which they appear, sliced
                in chunks of 10 if more than 10 are given.

            caption (`str`, optional):
                Optional caption for the sent media message. When sending an
                album, the caption may be a list of strings, which will be
                assigned to the files pairwise.

            force_document (`bool`, optional):
                If left to ``False`` and the file is a path that ends with
                the extension of an image file or a video file, it will be
                sent as such. Otherwise always as a document.

            progress_callback (`callable`, optional):
                A callback function accepting two parameters:
                ``(sent bytes, total)``.

            reply_to (`int` | `Message <telethon.tl.custom.message.Message>`):
                Same as `reply_to` from `send_message`.

            attributes (`list`, optional):
                Optional attributes that override the inferred ones, like
                :tl:`DocumentAttributeFilename` and so on.

            thumb (`str` | `bytes` | `file`, optional):
                Optional JPEG thumbnail (for documents). **Telegram will
                ignore this parameter** unless you pass a ``.jpg`` file!

                The file must also be small in dimensions and in-disk size.
                Successful thumbnails were files below 20kb and 200x200px.
                Width/height and dimensions/size ratios may be important.

            allow_cache (`bool`, optional):
                Whether to allow using the cached version stored in the
                database or not. Defaults to ``True`` to avoid re-uploads.
                Must be ``False`` if you wish to use different attributes
                or thumb than those that were used when the file was cached.

            parse_mode (`object`, optional):
                See the `TelegramClient.parse_mode
                <telethon.client.messageparse.MessageParseMethods.parse_mode>`
                property for allowed values. Markdown parsing will be used by
                default.

            voice_note (`bool`, optional):
                If ``True`` the audio will be sent as a voice note.

                Set `allow_cache` to ``False`` if you sent the same file
                without this setting before for it to work.

            video_note (`bool`, optional):
                If ``True`` the video will be sent as a video note,
                also known as a round video message.

                Set `allow_cache` to ``False`` if you sent the same file
                without this setting before for it to work.

            buttons (`list`, `custom.Button <telethon.tl.custom.button.Button>`, :tl:`KeyboardButton`):
                The matrix (list of lists), row list or button to be shown
                after sending the message. This parameter will only work if
                you have signed in as a bot. You can also pass your own
                :tl:`ReplyMarkup` here.

            silent (`bool`, optional):
                Whether the message should notify people in a broadcast
                channel or not. Defaults to ``False``, which means it will
                notify them. Set it to ``True`` to alter this behaviour.

            supports_streaming (`bool`, optional):
                Whether the sent video supports streaming or not. Note that
                Telegram only recognizes as streamable some formats like MP4,
                and others like AVI or MKV will not work. You should convert
                these to MP4 before sending if you want them to be streamable.
                Unsupported formats will result in ``VideoContentTypeError``.

        Notes:
            If the ``hachoir3`` package (``hachoir`` module) is installed,
            it will be used to determine metadata from audio and video files.

            If the `pillow` package is installed and you are sending a photo,
            it will be resized to fit within the maximum dimensions allowed
            by Telegram to avoid ``errors.PhotoInvalidDimensionsError``. This
            cannot be done if you are sending :tl:`InputFile`, however.

        Returns:
            The `telethon.tl.custom.message.Message` (or messages) containing
            the sent file, or messages if a list of them was passed.
        """
        # i.e. ``None`` was used
        if not file:
            raise TypeError('Cannot use {!r} as file'.format(file))

        if not caption:
            caption = ''

        # First check if the user passed an iterable, in which case
        # we may want to send as an album if all are photo files.
        if utils.is_list_like(file):
            # TODO Fix progress_callback
            images = []
            if force_document:
                documents = file
            else:
                documents = []
                for x in file:
                    if utils.is_image(x):
                        images.append(x)
                    else:
                        documents.append(x)

            result = []
            while images:
                result += await self._send_album(
                    entity, images[:10], caption=caption,
                    progress_callback=progress_callback, reply_to=reply_to,
                    parse_mode=parse_mode, silent=silent
                )
                images = images[10:]

            for x in documents:
                result.append(await self.send_file(
                    entity, x, allow_cache=allow_cache,
                    caption=caption, force_document=force_document,
                    progress_callback=progress_callback, reply_to=reply_to,
                    attributes=attributes, thumb=thumb, voice_note=voice_note,
                    video_note=video_note, buttons=buttons, silent=silent,
                    supports_streaming=supports_streaming,
                    **kwargs
                ))

            return result

        entity = await self.get_input_entity(entity)
        reply_to = utils.get_message_id(reply_to)

        # Not document since it's subject to change.
        # Needed when a Message is passed to send_message and it has media.
        if 'entities' in kwargs:
            msg_entities = kwargs['entities']
        else:
            caption, msg_entities =\
                await self._parse_message_text(caption, parse_mode)

        file_handle, media, image = await self._file_to_media(
            file, force_document=force_document,
            progress_callback=progress_callback,
            attributes=attributes,  allow_cache=allow_cache, thumb=thumb,
            voice_note=voice_note, video_note=video_note,
            supports_streaming=supports_streaming
        )

        # e.g. invalid cast from :tl:`MessageMediaWebPage`
        if not media:
            raise TypeError('Cannot use {!r} as file'.format(file))

        markup = self.build_reply_markup(buttons)
        request = functions.messages.SendMediaRequest(
            entity, media, reply_to_msg_id=reply_to, message=caption,
            entities=msg_entities, reply_markup=markup, silent=silent
        )
        msg = self._get_response_message(request, await self(request), entity)
        await self._cache_media(msg, file, file_handle, image=image)

        return msg

    async def _send_album(self, entity, files, caption='',
                          progress_callback=None, reply_to=None,
                          parse_mode=(), silent=None):
        """Specialized version of .send_file for albums"""
        # We don't care if the user wants to avoid cache, we will use it
        # anyway. Why? The cached version will be exactly the same thing
        # we need to produce right now to send albums (uploadMedia), and
        # cache only makes a difference for documents where the user may
        # want the attributes used on them to change.
        #
        # In theory documents can be sent inside the albums but they appear
        # as different messages (not inside the album), and the logic to set
        # the attributes/avoid cache is already written in .send_file().
        entity = await self.get_input_entity(entity)
        if not utils.is_list_like(caption):
            caption = (caption,)

        captions = []
        for c in reversed(caption):  # Pop from the end (so reverse)
            captions.append(await self._parse_message_text(c or '', parse_mode))

        reply_to = utils.get_message_id(reply_to)

        # Need to upload the media first, but only if they're not cached yet
        media = []
        for file in files:
            # Albums want :tl:`InputMedia` which, in theory, includes
            # :tl:`InputMediaUploadedPhoto`. However using that will
            # make it `raise MediaInvalidError`, so we need to upload
            # it as media and then convert that to :tl:`InputMediaPhoto`.
            fh, fm, _ = await self._file_to_media(file)
            if isinstance(fm, types.InputMediaUploadedPhoto):
                r = await self(functions.messages.UploadMediaRequest(
                    entity, media=fm
                ))
                self.session.cache_file(
                    fh.md5, fh.size, utils.get_input_photo(r.photo))

                fm = utils.get_input_media(r.photo)

            if captions:
                caption, msg_entities = captions.pop()
            else:
                caption, msg_entities = '', None
            media.append(types.InputSingleMedia(
                fm,
                message=caption,
                entities=msg_entities
            ))

        # Now we can construct the multi-media request
        result = await self(functions.messages.SendMultiMediaRequest(
            entity, reply_to_msg_id=reply_to, multi_media=media, silent=silent
        ))
        return [
            self._get_response_message(update.id, result, entity)
            for update in result.updates
            if isinstance(update, types.UpdateMessageID)
        ]

    async def upload_file(
            self, file, *, part_size_kb=None, file_name=None, use_cache=None,
            progress_callback=None):
        """
        Uploads the specified file and returns a handle (an instance of
        :tl:`InputFile` or :tl:`InputFileBig`, as required) which can be
        later used before it expires (they are usable during less than a day).

        Uploading a file will simply return a "handle" to the file stored
        remotely in the Telegram servers, which can be later used on. This
        will **not** upload the file to your own chat or any chat at all.

        Args:
            file (`str` | `bytes` | `file`):
                The path of the file, byte array, or stream that will be sent.
                Note that if a byte array or a stream is given, a filename
                or its type won't be inferred, and it will be sent as an
                "unnamed application/octet-stream".

            part_size_kb (`int`, optional):
                Chunk size when uploading files. The larger, the less
                requests will be made (up to 512KB maximum).

            file_name (`str`, optional):
                The file name which will be used on the resulting InputFile.
                If not specified, the name will be taken from the ``file``
                and if this is not a ``str``, it will be ``"unnamed"``.

            use_cache (`type`, optional):
                The type of cache to use (currently either :tl:`InputDocument`
                or :tl:`InputPhoto`). If present and the file is small enough
                to need the MD5, it will be checked against the database,
                and if a match is found, the upload won't be made. Instead,
                an instance of type ``use_cache`` will be returned.

            progress_callback (`callable`, optional):
                A callback function accepting two parameters:
                ``(sent bytes, total)``.

        Returns:
            :tl:`InputFileBig` if the file size is larger than 10MB,
            `telethon.tl.custom.inputsizedfile.InputSizedFile`
            (subclass of :tl:`InputFile`) otherwise.
        """
        if isinstance(file, (types.InputFile, types.InputFileBig)):
            return file  # Already uploaded

        if not file_name and getattr(file, 'name', None):
            file_name = file.name

        if isinstance(file, str):
            file_size = os.path.getsize(file)
        elif isinstance(file, bytes):
            file_size = len(file)
        else:
            if isinstance(file, io.IOBase) and file.seekable():
                pos = file.tell()
            else:
                pos = None

            # TODO Don't load the entire file in memory always
            data = file.read()
            if pos is not None:
                file.seek(pos)

            file = data
            file_size = len(file)

        # File will now either be a string or bytes
        if not part_size_kb:
            part_size_kb = utils.get_appropriated_part_size(file_size)

        if part_size_kb > 512:
            raise ValueError('The part size must be less or equal to 512KB')

        part_size = int(part_size_kb * 1024)
        if part_size % 1024 != 0:
            raise ValueError(
                'The part size must be evenly divisible by 1024')

        # Set a default file name if None was specified
        file_id = helpers.generate_random_long()
        if not file_name:
            if isinstance(file, str):
                file_name = os.path.basename(file)
            else:
                file_name = str(file_id)

        # If the file name lacks extension, add it if possible.
        # Else Telegram complains with `PHOTO_EXT_INVALID_ERROR`
        # even if the uploaded image is indeed a photo.
        if not os.path.splitext(file_name)[-1]:
            file_name += utils._get_extension(file)

        # Determine whether the file is too big (over 10MB) or not
        # Telegram does make a distinction between smaller or larger files
        is_large = file_size > 10 * 1024 * 1024
        hash_md5 = hashlib.md5()
        if not is_large:
            # Calculate the MD5 hash before anything else.
            # As this needs to be done always for small files,
            # might as well do it before anything else and
            # check the cache.
            if isinstance(file, str):
                with open(file, 'rb') as stream:
                    file = stream.read()
            hash_md5.update(file)
            if use_cache:
                cached = self.session.get_file(
                    hash_md5.digest(), file_size, cls=_CacheType(use_cache)
                )
                if cached:
                    return cached

        part_count = (file_size + part_size - 1) // part_size
        self._log[__name__].info('Uploading file of %d bytes in %d chunks of %d',
                                 file_size, part_count, part_size)

        with open(file, 'rb') if isinstance(file, str) else BytesIO(file)\
                as stream:
            for part_index in range(part_count):
                # Read the file by in chunks of size part_size
                part = stream.read(part_size)

                # The SavePartRequest is different depending on whether
                # the file is too large or not (over or less than 10MB)
                if is_large:
                    request = functions.upload.SaveBigFilePartRequest(
                        file_id, part_index, part_count, part)
                else:
                    request = functions.upload.SaveFilePartRequest(
                        file_id, part_index, part)

                result = await self(request)
                if result:
                    self._log[__name__].debug('Uploaded %d/%d',
                                              part_index + 1, part_count)
                    if progress_callback:
                        progress_callback(stream.tell(), file_size)
                else:
                    raise RuntimeError(
                        'Failed to upload file part {}.'.format(part_index))

        if is_large:
            return types.InputFileBig(file_id, part_count, file_name)
        else:
            return custom.InputSizedFile(
                file_id, part_count, file_name, md5=hash_md5, size=file_size
            )

    # endregion

    async def _file_to_media(
            self, file, force_document=False,
            progress_callback=None, attributes=None, thumb=None,
            allow_cache=True, voice_note=False, video_note=False,
            supports_streaming=False):
        if not file:
            return None, None, None

        if isinstance(file, pathlib.Path):
            file = str(file.absolute())

        as_image = utils.is_image(file) and not force_document

        if not isinstance(file, (str, bytes, io.IOBase)):
            # The user may pass a Message containing media (or the media,
            # or anything similar) that should be treated as a file. Try
            # getting the input media for whatever they passed and send it.
            #
            # We pass all attributes since these will be used if the user
            # passed :tl:`InputFile`, and all information may be relevant.
            try:
                return (None, utils.get_input_media(
                    file,
                    is_photo=as_image,
                    attributes=attributes,
                    force_document=force_document,
                    voice_note=voice_note,
                    video_note=video_note,
                    supports_streaming=supports_streaming
                ), as_image)
            except TypeError:
                # Can't turn whatever was given into media
                return None, None, as_image

        media = None
        file_handle = None
        use_cache = types.InputPhoto if as_image else types.InputDocument
        if not isinstance(file, str) or os.path.isfile(file):
            file_handle = await self.upload_file(
                _resize_photo_if_needed(file, as_image),
                progress_callback=progress_callback,
                use_cache=use_cache if allow_cache else None
            )
        elif re.match('https?://', file):
            if as_image:
                media = types.InputMediaPhotoExternal(file)
            elif not force_document and utils.is_gif(file):
                media = types.InputMediaGifExternal(file, '')
            else:
                media = types.InputMediaDocumentExternal(file)
        else:
            bot_file = utils.resolve_bot_file_id(file)
            if bot_file:
                media = utils.get_input_media(bot_file)

        if media:
            pass  # Already have media, don't check the rest
        elif not file_handle:
            raise ValueError(
                'Failed to convert {} to media. Not an existing file, '
                'an HTTP URL or a valid bot-API-like file ID'.format(file)
            )
        elif isinstance(file_handle, use_cache):
            # File was cached, so an instance of use_cache was returned
            if as_image:
                media = types.InputMediaPhoto(file_handle)
            else:
                media = types.InputMediaDocument(file_handle)
        elif as_image:
            media = types.InputMediaUploadedPhoto(file_handle)
        else:
            attributes, mime_type = utils.get_attributes(
                file,
                attributes=attributes,
                force_document=force_document,
                voice_note=voice_note,
                video_note=video_note,
                supports_streaming=supports_streaming
            )

            input_kw = {}
            if thumb:
                if isinstance(thumb, pathlib.Path):
                    thumb = str(thumb.absolute())
                input_kw['thumb'] = await self.upload_file(thumb)

            media = types.InputMediaUploadedDocument(
                file=file_handle,
                mime_type=mime_type,
                attributes=attributes,
                **input_kw
            )
        return file_handle, media, as_image

    async def _cache_media(self, msg, file, file_handle, image):
        if file and msg and isinstance(file_handle,
                                       custom.InputSizedFile):
            # There was a response message and we didn't use cached
            # version, so cache whatever we just sent to the database.
            md5, size = file_handle.md5, file_handle.size
            if image:
                to_cache = utils.get_input_photo(msg.media.photo)
            else:
                to_cache = utils.get_input_document(msg.media.document)
            self.session.cache_file(md5, size, to_cache)

    # endregion
