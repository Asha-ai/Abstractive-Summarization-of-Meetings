# Copyright 2019 The Texar Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modifications copyright (C) 2020 Bastian Oppermann
# The original, unmodified file(s) can be found at
# https://github.com/asyml/texar/blob/413e07f859acbbee979f274b52942edd57b335c1/examples/transformer/transformer_main.py#
# and
# https://github.com/asyml/texar/blob/413e07f859acbbee979f274b52942edd57b335c1/examples/bert/bert_classifier_main.py
import os
import tensorflow as tf
import texar.tf as tx
from texar.tf.modules import TransformerDecoder, BERTEncoder
from texar.tf.utils import transformer_utils
from bleu_tool import bleu_wrapper

import config_model
import config_data

from utils import utils
from utils.data_utils import bos_token_id, eos_token_id


flags = tf.flags

flags.DEFINE_string("run_mode", "train_and_evaluate", "Either train_and_evaluate or test.")

FLAGS = flags.FLAGS

model_dir = './outputs'


def get_data_iterator():
    train_dataset = tx.data.TFRecordData(hparams=config_data.train_hparam)
    eval_dataset = tx.data.TFRecordData(hparams=config_data.eval_hparam)
    test_dataset = tx.data.TFRecordData(hparams=config_data.test_hparam)

    iterator = tx.data.FeedableDataIterator({'train': train_dataset, 'eval': eval_dataset, 'test': test_dataset})

    return iterator


def main():
    data_iterator = get_data_iterator()
    batch = data_iterator.get_next()

    src_input_ids = batch['src_input_ids']
    src_segment_ids = batch['src_segment_ids']
    src_input_mask = batch['src_input_mask']
    tgt_input_ids = batch['tgt_input_ids']
    tgt_input_mask = batch['tgt_input_mask']
    tgt_labels = batch['tgt_labels']

    is_target = tf.cast(tf.not_equal(tgt_labels, 0), tf.float32)

    batch_size = tf.shape(src_input_ids)[0]
    input_length = tf.reduce_sum(1 - tf.cast(tf.equal(src_input_ids, 0), tf.int32), axis=1)

    beam_width = config_model.beam_width

    encoder = BERTEncoder(pretrained_model_name=config_model.bert['pretrained_model_name'])
    encoder_output, encoder_pooled_output = encoder(inputs=src_input_ids,
                                                    segment_ids=src_segment_ids,
                                                    sequence_length=input_length)

    vocab_size = BERTEncoder.default_hparams()['vocab_size']

    src_word_embedder = encoder.word_embedder
    pos_embedder = encoder.position_embedder

    tgt_embedding = tf.concat(
        [tf.zeros(shape=[1, src_word_embedder.dim]),
         src_word_embedder.embedding[1:, :]],
        axis=0)
    tgt_embedder = tx.modules.WordEmbedder(tgt_embedding)
    tgt_word_embeds = tgt_embedder(tgt_input_ids)
    tgt_word_embeds = tgt_word_embeds * config_model.hidden_dim ** 0.5

    tgt_seq_len = tf.ones([batch_size], tf.int32) * tf.shape(tgt_input_ids)[1]
    tgt_pos_embeds = pos_embedder(sequence_length=tgt_seq_len)

    tgt_input_embedding = tgt_word_embeds + tgt_pos_embeds

    _output_w = tf.transpose(tgt_embedder.embedding, (1, 0))

    decoder = TransformerDecoder(vocab_size=vocab_size,
                                 output_layer=_output_w,
                                 hparams=config_model.decoder)

    # For training
    decoder_outputs = decoder(
        memory=encoder_output,
        memory_sequence_length=input_length,
        inputs=tgt_input_embedding,
        decoding_strategy='train_greedy',
        mode=tf.estimator.ModeKeys.TRAIN
    )

    mle_loss = transformer_utils.smoothing_cross_entropy(
        decoder_outputs.logits, tgt_labels, vocab_size, config_model.loss_label_confidence)
    mle_loss = tf.reduce_sum(mle_loss * is_target) / tf.reduce_sum(is_target)

    global_step = tf.Variable(0, dtype=tf.int64, trainable=False)
    learning_rate = tf.placeholder(tf.float64, shape=(), name='lr')

    train_op = tx.core.get_train_op(
        mle_loss,
        learning_rate=learning_rate,
        global_step=global_step,
        hparams=config_model.opt)

    tf.summary.scalar('lr', learning_rate)
    tf.summary.scalar('mle_loss', mle_loss)
    summary_merged = tf.summary.merge_all()

    # For inference (beam-search)
    start_tokens = tf.fill([batch_size], bos_token_id)

    saver = tf.train.Saver(max_to_keep=5)
    best_results = {'score': 0, 'epoch': -1}

    def _embedding_fn(x, y):
        x_w_embed = tgt_embedder(x)
        y_p_embed = pos_embedder(y)
        return x_w_embed * config_model.hidden_dim ** 0.5 + y_p_embed

    predictions = decoder(
        memory=encoder_output,
        memory_sequence_length=input_length,
        beam_width=beam_width,
        start_tokens=start_tokens,
        end_token=eos_token_id,
        embedding=_embedding_fn,
        max_decoding_length=config_data.max_decoding_length,
        decoding_strategy='infer_greedy',
        mode=tf.estimator.ModeKeys.PREDICT)

    # Uses the best sample by beam search
    beam_search_ids = predictions['sample_id'][:, :, 0]

    def _train_epoch(sess, epoch, step, smry_writer):
        print('Start epoch %d' % epoch)
        data_iterator.restart_dataset(sess, 'train')

        fetches = {
            'train_op': train_op,
            'loss': mle_loss,
            'step': global_step,
            'smry': summary_merged
        }

        while True:
            try:
                feed_dict = {
                    data_iterator.handle: data_iterator.get_handle(sess, 'train'),
                    tx.global_mode(): tf.estimator.ModeKeys.TRAIN,
                    learning_rate: utils.get_lr(step, config_model.lr)
                }

                fetches_ = sess.run(fetches, feed_dict)
                step, loss = fetches_['step'], fetches_['loss']

                # Display every display_steps
                display_steps = config_data.display_steps
                if display_steps > 0 and step % display_steps == 0:
                    print('step: %d, loss: %.4f' % (step, loss))
                    smry_writer.add_summary(fetches_['smry'], global_step=step)

                # Eval every eval_steps
                eval_steps = config_data.eval_steps
                if eval_steps > 0 and step % eval_steps == 0 and step > 0:
                    _eval_epoch(sess, epoch, 'eval')

            except tf.errors.OutOfRangeError:
                break

        return step

    def _eval_epoch(sess, epoch, mode):
        print('Starting eval')
        data_iterator.restart_dataset(sess, 'eval')
        references, hypotheses = [], []

        while True:
            try:
                feed_dict = {
                    data_iterator.handle: data_iterator.get_handle(sess, 'eval'),
                    tx.global_mode(): tf.estimator.ModeKeys.EVAL,
                }
                fetches = {
                    'beam_search_ids': beam_search_ids,
                    'tgt_input_ids': tgt_input_ids
                }
                fetches_ = sess.run(fetches, feed_dict=feed_dict)

                hypotheses.extend(h.tolist() for h in fetches_['beam_search_ids'])
                references.extend(r.tolist() for r in fetches_['tgt_input_ids'])
                hypotheses = utils.list_strip_eos(hypotheses, eos_token_id)
                references = utils.list_strip_eos(references, eos_token_id)
            except tf.errors.OutOfRangeError:
                break

        if mode == 'eval':
            # Writes results to files to evaluate BLEU
            # For 'eval' mode, the BLEU is based on token ids (rather than
            # text tokens) and serves only as a surrogate metric to monitor
            # the training process
            fname = os.path.join(model_dir, 'tmp.eval')
            hypotheses = tx.utils.str_join(hypotheses)
            references = tx.utils.str_join(references)
            hyp_fn, ref_fn = tx.utils.write_paired_text(
                hypotheses, references, fname, mode='s')

            print(hyp_fn)

            eval_bleu = bleu_wrapper(ref_fn, hyp_fn, case_sensitive=True)
            print('epoch: %d, eval_bleu %.4f' % (epoch, eval_bleu))

            if eval_bleu > best_results['score']:
                best_results['score'] = eval_bleu
                best_results['epoch'] = epoch
                model_path = os.path.join(model_dir, 'best-model.ckpt')
                print('saving model to %s' % model_path)

                # Also save the best results in a text file
                tx.utils.write_paired_text(hypotheses, references, os.path.join(model_dir, 'best.eval'), mode='s')

                saver.save(sess, model_path)

        elif mode == 'test':
            print('Test not yet implemented!')
            # TODO

    # Run the graph
    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        sess.run(tf.local_variables_initializer())
        sess.run(tf.tables_initializer())

        smry_writer = tf.summary.FileWriter(model_dir, graph=sess.graph)

        if FLAGS.run_mode == 'train_and_evaluate':
            print('Begin running with train_and_evaluate mode')

            if tf.train.latest_checkpoint(model_dir) is not None:
                print('Restore latest checkpoint in %s' % model_dir)
                saver.restore(sess, tf.train.latest_checkpoint(model_dir))

            step = 0
            for epoch in range(config_data.max_train_epoch):
                step = _train_epoch(sess, epoch, step, smry_writer)

        elif FLAGS.run_mode == 'test':
            print('Begin running with test mode')

            print('Restore latest checkpoint in %s' % model_dir)
            saver.restore(sess, tf.train.latest_checkpoint(model_dir))

            _eval_epoch(sess, 0, mode='test')

        else:
            raise ValueError('Unknown mode: {}'.format(FLAGS.run_mode))


if __name__ == '__main__':
    main()