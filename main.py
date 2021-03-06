from model import *
import cv2
import time


def preprocess_test_images(left_image, right_image):
    with tf.variable_scope("preprocess", reuse=True):
        # flip images
        should_flip = tf.random_uniform([], 0, 1)
        left_image, right_image = tf.cond(
            tf.greater(should_flip, 0.5),
            lambda: [tf.image.flip_left_right(right_image), tf.image.flip_left_right(left_image)],
            lambda: [left_image, right_image])

        # augment images
        should_augment = tf.Variable(tf.random_uniform([], 0, 1), trainable=False)
        left_image, right_image = tf.cond(
            tf.greater(should_augment, 0.5),
            lambda: augment(left_image, right_image),
            lambda: (left_image, right_image))

        left_image.set_shape([None, None, 3])
        right_image.set_shape([None, None, 3])

        return left_image, right_image


def augment(left_image, right_image):
    with tf.variable_scope("augment", reuse=True):
        # shift gamma
        random_gamma = tf.random_uniform([], 0.8, 1.2)
        left_image_aug = left_image ** random_gamma
        right_image_aug = right_image ** random_gamma

        # shift brightness
        random_brightness = tf.random_uniform([], 0.5, 2.0)
        left_image_aug = left_image_aug * random_brightness
        right_image_aug = right_image_aug * random_brightness

        # shift color
        random_colors = tf.random_uniform([3], 0.8, 1.2)
        shape = tf.shape(left_image)
        white = tf.ones([shape[0], shape[1]])
        color_mask = tf.stack([white * random_colors[i] for i in range(3)], axis=2)
        left_image_aug *= color_mask
        right_image_aug *= color_mask

        # normalize
        left_image_aug = tf.clip_by_value(left_image_aug, 0, 1)
        right_image_aug = tf.clip_by_value(right_image_aug, 0, 1)

        return left_image_aug, right_image_aug


def read_images(files, input_width=512, input_height=512, batch_size=32):
    with tf.variable_scope("load_images", reuse=True):
        input_queue = tf.train.string_input_producer(files, shuffle=False)
        reader = tf.TextLineReader()
        _, path = reader.read(input_queue)

        splits = tf.string_split([path], ";").values
        # splits = tf.Print(splits, [splits], message="Batch values")
        left_image = tf.image.convert_image_dtype(tf.image.decode_jpeg(tf.read_file(splits[0])), tf.float32)
        right_image = tf.image.convert_image_dtype(tf.image.decode_jpeg(tf.read_file(splits[1])), tf.float32)

        left_image = tf.image.resize_images(left_image, [input_height, input_width], tf.image.ResizeMethod.AREA)
        right_image = tf.image.resize_images(right_image, [input_height, input_width], tf.image.ResizeMethod.AREA)

        return tf.train.shuffle_batch(
            preprocess_test_images(left_image, right_image),
            batch_size,
            128 + 4 * batch_size,
            128,
            4,
            allow_smaller_final_batch=True)


def count_lines(filenames):
    counter = 0
    for filename in filenames:
        with open(filename) as file:
            for lines in file:
                counter += 1

    return counter


def test(checkpoint_path=""):
    test_filenames = ["kitti/test.txt"]
    test_length = count_lines(test_filenames)

    BATCH_SIZE = 1

    left, right = read_images(test_filenames, batch_size=BATCH_SIZE)

    conv1, output_left, output_right = model(left)

    config = tf.ConfigProto(allow_soft_placement=True)
    with tf.Session(config=config) as sess:
        loader = tf.train.Saver()

        sess.run(tf.global_variables_initializer())
        sess.run(tf.local_variables_initializer())
        coordinator = tf.train.Coordinator()
        tf.train.start_queue_runners(sess=sess, coord=coordinator)
        loader.recover_last_checkpoints([checkpoint_path])

        print('now testing {} files'.format(test_length))
        disparities = np.zeros((test_length, 512, 512), dtype=np.float32)
        conv = None
        for step in range(test_length):
            disp, conv = sess.run([output_left[0], conv1])
            disparities[step] = disp[0].squeeze()
            ## disparities_pp[step] = post_process_disparity(disp.squeeze())

        print('done.')

        print(conv)
        print('showing disparities.')
        for i in range(0, conv.shape[3]):
            img = conv[0, :, :, i:i+1]
            img = np.int32((img / np.max(img)) * 255)
            cv2.imwrite("img/image_%d.jpg" % i, img)


def train(run_num):
    tf.logging.set_verbosity(tf.logging.WARN)
    logdir = "logs/run%d" % run_num

    train_filenames = ["kitti/train.txt"]
    train_length = count_lines(train_filenames)
    test_filenames = ["kitti/test.txt"]

    rate = 1e-4

    EPOCHS = 50
    BATCH_SIZE = 8

    total_steps = (train_length // BATCH_SIZE) * EPOCHS
    print("Total steps: %d" % total_steps)

    global_step = tf.Variable(0, trainable=False)

    boundaries = [np.int32((3 / 5.0) * total_steps), np.int32((4 / 5.0) * total_steps)]
    values = [rate, rate / 2, rate / 4]
    rate = tf.train.piecewise_constant(global_step, boundaries, values)

    optimizer = tf.train.AdamOptimizer(rate)

    batch_x, batch_y = read_images(train_filenames, batch_size=BATCH_SIZE)

    with tf.device('/gpu:0'):
        outputs, outputs_left, outputs_right = model(batch_x)
        tower_loss = loss(outputs_left, outputs_right, batch_x, batch_y)
        tower_grad = optimizer.compute_gradients(tower_loss)

    grads_apply_op = optimizer.apply_gradients(tower_grad, global_step=global_step)
    loss_op = tower_loss

    tf.summary.scalar('learning_rate', rate, ['model'])
    tf.summary.scalar('loss', loss_op, ['model'])
    summary(outputs_left, outputs_right, batch_x, batch_y)
    summary_op = tf.summary.merge_all('model')

    config = tf.ConfigProto(allow_soft_placement=True)
    with tf.Session(config=config) as sess:
        saver = tf.train.Saver()
        summary_writer = tf.summary.FileWriter(logdir, sess.graph)

        sess.run(tf.local_variables_initializer())
        sess.run(tf.global_variables_initializer())

        coordinator = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=sess, coord=coordinator)

        start_step = global_step.eval(session=sess)
        start_time = time.time()
        for step in range(start_step, total_steps):
            before_op_time = time.time()
            _, loss_value = sess.run([grads_apply_op, loss_op])
            duration = time.time() - before_op_time
            if step:
                examples_per_sec = BATCH_SIZE / duration
                time_so_far = (time.time() - start_time)
                training_time_left = (total_steps / step - 1.0) * time_so_far
                print_string = 'step {:>6} | examples/s: {:4.2f} | loss: {:.5f} | time elapsed: {:.2f}s | time left: {:.2f}s'
                print(print_string.format(step, examples_per_sec, loss_value, time_so_far, training_time_left))

                if step % 1 == 0:# (total_steps // (EPOCHS * 3)):
                    summary_str = sess.run(summary_op)
                    summary_writer.add_summary(summary_str, global_step=step)
                    saver.save(sess, logdir + '/model.cpkt', global_step=step)

        saver.save(sess, logdir + '/model.cpkt', global_step=EPOCHS)

        coordinator.request_stop()
        coordinator.join(threads)

train(2)
#test("logs/run2/checkpoint")
