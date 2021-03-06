from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys
from six.moves import urllib
import tensorflow as tf

import notmnist_input

FLAGS = tf.app.flags.FLAGS

tf.app.flags.DEFINE_integer('batch_size',40,
							"""Number of images to process in one batch""")

IMAGE_SIZE = 28
NUM_CLASSES = 8

MOVING_AVERAGE_DECAY = 0.9999
NUM_EPOCHS_PER_DECAY = 100.0
LEARNING_RATE_DECAY_FACTOR = 0.1
INITIAL_LEARNING_RATE = 0.1

TOWER_NAME = 'tower'

def _activation_summary(x):
	tf.summary.histogram(x.op.name+'/activation',x)
	tf.summary.scalar(x.op.name+'/sparsity',tf.nn.zero_fraction(x))

def _variable_on_cpu(name,shape,initializer):
	with tf.device('/cpu:0'):
		dtype = tf.float32
		var = tf.get_variable(name,shape,initializer=initializer)
	return var

def _variable_with_weight_decay(name,shape,stddev,wd):
	dtype = tf.float32
	var = _variable_on_cpu(name,shape,tf.truncated_normal_initializer(stddev=stddev,dtype=dtype))
	if wd is not None:
		weight_decay = tf.multiply(tf.nn.l2_loss(var),wd,name='weight_loss')
		tf.add_to_collection('losses',weight_decay)
	return var

def inputs(iseval):
	num_examples,images,labels = notmnist_input.read_data(batch_size=FLAGS.batch_size,shuffle=True,iseval=iseval)
	return num_examples,images,labels

def inference(images):
	with tf.variable_scope('conv1') as scope:
		kernel = _variable_with_weight_decay('weights',
											shape=[5,5,1,16],
											stddev=5e-2,
											wd=None)
		conv = tf.nn.conv2d(images,kernel,[1,1,1,1],padding='SAME')
		biases = _variable_on_cpu('biases',[16],tf.constant_initializer(0.0))
		pre_activation = tf.nn.bias_add(conv,biases)
		conv1 = tf.nn.relu(pre_activation,name=scope.name)
		_activation_summary(conv1)

	pool1 = tf.nn.max_pool(conv1,ksize=[1,3,3,1],strides=[1,2,2,1],
							padding='SAME',name='pool1')
	norm1 = tf.nn.lrn(pool1,4,bias=1.0,alpha=0.001/9.0,beta=0.75,name='norm1')

	with tf.variable_scope('conv2') as scope:
		kernel = _variable_with_weight_decay('weights',
											shape=[3,3,16,32],
											stddev=5e-2,
											wd=None)
		conv = tf.nn.conv2d(norm1,kernel,[1,1,1,1],padding='SAME')
		biases = _variable_on_cpu('biases',[32],tf.constant_initializer(0.1))
		pre_activation = tf.nn.bias_add(conv,biases)
		conv2 = tf.nn.relu(pre_activation,name=scope.name)
		_activation_summary(conv2)

	
	norm2 = tf.nn.lrn(conv2,4,bias=1.0,alpha=0.001/9.0,beta=0.75,name='norm2')
	pool2 = tf.nn.max_pool(norm2,ksize=[1,3,3,1],strides=[1,2,2,1],
							padding='SAME',name='pool2')

	with tf.variable_scope('linear1') as scope:
		reshape = tf.reshape(pool2,[FLAGS.batch_size,-1])
		dim = reshape.get_shape()[1].value
		weights = _variable_with_weight_decay('weights',shape=[dim,256],stddev=0.04,wd=0.004)
		biases = _variable_on_cpu('biases',[256],tf.constant_initializer(0.1))
		linear1 = tf.nn.relu(tf.matmul(reshape,weights)+biases,name=scope.name)
		_activation_summary(linear1)

	with tf.variable_scope('linear2') as scope:
		weights = _variable_with_weight_decay('weights',shape=[256,128],stddev=0.04,wd=0.004)
		biases = _variable_on_cpu('biases',[128],tf.constant_initializer(0.1))
		linear2 = tf.nn.relu(tf.matmul(linear1,weights)+biases,name=scope.name)
		_activation_summary(linear2)

	with tf.variable_scope('softmax') as scope:
		weights = _variable_with_weight_decay('weights',shape=[128,8],stddev=1/128.0,wd=None)
		biases = _variable_on_cpu('biases',[8],tf.constant_initializer(0.0))
		softmax = tf.add(tf.matmul(linear2,weights),biases,name=scope.name)
		_activation_summary(softmax)

	return softmax

def loss(logits,labels):
	labels = tf.cast(labels,tf.int64)
	cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(
			labels=labels,logits=logits,name='cross_entropy_per_example')
	cross_entropy_mean = tf.reduce_mean(cross_entropy,name='cross_entropy')
	tf.add_to_collection('losses',cross_entropy_mean)

	return tf.add_n(tf.get_collection('losses'),name='total_loss')

def accuracy(logits,labels):
	with tf.name_scope('accuracy'):
		correct = tf.equal(tf.argmax(logits,1),labels)
		accur = tf.reduce_mean(tf.cast(correct,tf.float32))
		tf.summary.scalar('accuracy',accur)
	return accur

def _add_loss_summaries(total_loss):
	loss_averages = tf.train.ExponentialMovingAverage(0.9,name='avg')
	losses = tf.get_collection('losses')
	loss_averages_op = loss_averages.apply(losses+[total_loss])

	for l in losses+[total_loss]:
		tf.summary.scalar(l.op.name+'(raw)',l)
		tf.summary.scalar(l.op.name,loss_averages.average(l))

	return loss_averages_op

def train(total_loss,global_step,num_examples):
	num_batches_per_epoch = num_examples/FLAGS.batch_size
	decay_steps = int(num_batches_per_epoch*NUM_EPOCHS_PER_DECAY)

	lr = tf.train.exponential_decay(INITIAL_LEARNING_RATE,
									global_step,
									decay_steps,
									LEARNING_RATE_DECAY_FACTOR,
									staircase=True)
	tf.summary.scalar('learning_rate',lr)

	loss_averages_op = _add_loss_summaries(total_loss)

	with tf.control_dependencies([loss_averages_op]):
		opt = tf.train.GradientDescentOptimizer(lr)
		grads = opt.compute_gradients(total_loss)

	apply_gradient_op = opt.apply_gradients(grads,global_step=global_step)
	
	for var in tf.trainable_variables():
		tf.summary.histogram(var.op.name, var)

	for grad, var in grads:
		if grad is not None:
			tf.summary.histogram(var.op.name + '/gradients', grad)

	variable_averages = tf.train.ExponentialMovingAverage(
			MOVING_AVERAGE_DECAY, global_step)
	variables_averages_op = variable_averages.apply(tf.trainable_variables())

	with tf.control_dependencies([apply_gradient_op, variables_averages_op]):
		train_op = tf.no_op(name='train')

	return train_op